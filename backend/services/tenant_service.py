"""
租户配置服务
tenants 表真正启用：租户配置从平台库加载（带 TTL 缓存与主动失效），
未落库时回退到 PRESET_TENANTS 预置默认（保证种子脚本未运行时现有流程不断）
"""

import time
from typing import Any, Dict, List, Optional

from sqlalchemy import select

from backend.database import PlatformSessionLocal
from backend.models.tenant import Tenant
from backend.utils.logging import get_logger

logger = get_logger(__name__)

# 配置缓存：{tenant_id: (config_dict, expire_ts)}
# Demo 深度：进程内 TTL 缓存 + 写操作主动失效，足够单机演示
_CONFIG_CACHE: Dict[str, tuple] = {}
_CACHE_TTL_SECONDS = 60


def _row_to_config(row: Tenant) -> Dict[str, Any]:
    """ORM 行 → 对外的租户配置字典（结构与 PRESET_TENANTS 保持一致）"""
    return {
        "id": row.id,
        "name": row.name,
        "code": row.code,
        "ai_backend": row.ai_backend or {},
        "data_sources": row.data_sources or [],
        "regulation_config": row.regulation_config or {},
        "agent_config": row.agent_config or {},
    }


def invalidate_cache(tenant_id: Optional[str] = None):
    """主动失效缓存；tenant_id 为 None 时清空全部"""
    if tenant_id is None:
        _CONFIG_CACHE.clear()
    else:
        _CONFIG_CACHE.pop(tenant_id, None)


async def get_tenant_config(tenant_id: str) -> Optional[Dict[str, Any]]:
    """按 ID 加载租户配置：缓存 → 平台库 → PRESET_TENANTS 兜底"""
    now = time.monotonic()
    cached = _CONFIG_CACHE.get(tenant_id)
    if cached and cached[1] > now:
        return cached[0]

    async with PlatformSessionLocal() as session:
        row = await session.get(Tenant, tenant_id)

    if row and row.status == "active":
        config = _row_to_config(row)
    else:
        # 兜底：DB 无该租户（种子未运行）时回退预置默认，保证演示流程可用
        from backend.core.tenant_context import PRESET_TENANTS
        config = PRESET_TENANTS.get(tenant_id)

    if config:
        _CONFIG_CACHE[tenant_id] = (config, now + _CACHE_TTL_SECONDS)
    return config


async def list_all_tenants() -> List[Dict[str, Any]]:
    """列出平台库全部租户的摘要信息"""
    async with PlatformSessionLocal() as session:
        rows = (await session.execute(select(Tenant).order_by(Tenant.id))).scalars().all()
    return [
        {"id": r.id, "name": r.name, "code": r.code, "status": r.status}
        for r in rows
    ]


async def create_tenant(
    tenant_id: str,
    name: str,
    code: str,
    ai_backend: Optional[dict] = None,
    data_sources: Optional[list] = None,
    regulation_config: Optional[dict] = None,
    agent_config: Optional[dict] = None,
) -> Optional[Tenant]:
    """创建租户；ID 或 code 冲突返回 None"""
    async with PlatformSessionLocal() as session:
        conflict = await session.execute(
            select(Tenant).where((Tenant.id == tenant_id) | (Tenant.code == code))
        )
        if conflict.scalars().first():
            return None
        row = Tenant(
            id=tenant_id,
            name=name,
            code=code,
            status="active",
            ai_backend=ai_backend or {},
            data_sources=data_sources or [],
            regulation_config=regulation_config or {},
            agent_config=agent_config or {},
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
    invalidate_cache(tenant_id)
    return row


async def update_tenant(tenant_id: str, updates: Dict[str, Any]) -> Optional[Tenant]:
    """更新租户配置（仅更新传入的字段）；租户不存在返回 None"""
    allowed = {"name", "code", "status", "ai_backend", "data_sources",
               "regulation_config", "agent_config"}
    async with PlatformSessionLocal() as session:
        row = await session.get(Tenant, tenant_id)
        if not row:
            return None
        for key, value in updates.items():
            if key in allowed and value is not None:
                setattr(row, key, value)
        await session.commit()
        await session.refresh(row)
    invalidate_cache(tenant_id)
    return row


async def bind_user(tenant_id: str, user_id: str, role: str = "admin"):
    """把用户绑定为新租户成员（幂等）——创建租户后创建者立即可用"""
    from backend.models.user import UserTenantBinding

    async with PlatformSessionLocal() as session:
        exists = await session.execute(
            select(UserTenantBinding).where(
                UserTenantBinding.user_id == user_id,
                UserTenantBinding.tenant_id == tenant_id,
            )
        )
        if not exists.scalars().first():
            session.add(UserTenantBinding(
                user_id=user_id, tenant_id=tenant_id, role=role
            ))
            await session.commit()


async def seed_preset_tenants() -> List[str]:
    """把 PRESET_TENANTS 预置配置灌入 tenants 表（幂等：已存在则跳过）
    返回本次新写入的租户 ID 列表"""
    from backend.core.tenant_context import PRESET_TENANTS

    created = []
    async with PlatformSessionLocal() as session:
        for tid, cfg in PRESET_TENANTS.items():
            if await session.get(Tenant, tid):
                continue
            session.add(Tenant(
                id=tid,
                name=cfg["name"],
                code=cfg["code"],
                status="active",
                ai_backend=cfg.get("ai_backend", {}),
                data_sources=cfg.get("data_sources", []),
                regulation_config=cfg.get("regulation_config", {}),
                agent_config=cfg.get("agent_config", {}),
            ))
            created.append(tid)
        await session.commit()
    if created:
        invalidate_cache()
        logger.info("预置租户种子写入: %s", created)
    return created
