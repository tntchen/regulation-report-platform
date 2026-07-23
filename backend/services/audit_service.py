"""
审计服务
审计记录的写入（容错：审计失败不影响业务请求）与查询
"""

from typing import Optional, Dict, Any, List

from sqlalchemy import select, func

from backend.database import PlatformSessionLocal
from backend.models.audit_log import AuditLog
from backend.utils.logging import get_logger, trace_id_ctx

logger = get_logger(__name__)


async def write_audit(
    action: str,
    tenant_id: Optional[str] = None,
    user: Optional[Dict[str, Any]] = None,
    resource: Optional[str] = None,
    detail: Optional[Dict[str, Any]] = None,
    ip: Optional[str] = None,
    result: str = "success",
    duration_ms: Optional[int] = None,
):
    """写入一条审计记录
    容错设计：任何审计写库失败只记 error 日志，绝不向业务抛出"""
    try:
        # 敏感字段兜底过滤（密码/token 绝不落审计）
        safe_detail = dict(detail or {})
        for key in list(safe_detail.keys()):
            if any(s in key.lower() for s in ("password", "token", "secret", "api_key")):
                safe_detail[key] = "***"

        async with PlatformSessionLocal() as session:
            session.add(AuditLog(
                trace_id=trace_id_ctx.get("-"),
                user_id=user.get("id") if user else None,
                username=user.get("username") if user else None,
                tenant_id=tenant_id,
                action=action,
                resource=resource,
                detail=safe_detail,
                ip=ip,
                result=result,
                duration_ms=duration_ms,
            ))
            await session.commit()
    except Exception as e:
        logger.error("审计写入失败(action=%s): %s", action, e)


async def query_audit_logs(
    tenant_id: str,
    action: Optional[str] = None,
    username: Optional[str] = None,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
) -> Dict[str, Any]:
    """分页查询审计日志
    说明：登录等平台级动作无租户归属（tenant_id 为 NULL），一并纳入查询"""
    async with PlatformSessionLocal() as session:
        tenant_filter = (AuditLog.tenant_id == tenant_id) | (AuditLog.tenant_id.is_(None))
        stmt = select(AuditLog).where(tenant_filter)
        count_stmt = select(func.count(AuditLog.id)).where(tenant_filter)

        if action:
            stmt = stmt.where(AuditLog.action == action)
            count_stmt = count_stmt.where(AuditLog.action == action)
        if username:
            stmt = stmt.where(AuditLog.username == username)
            count_stmt = count_stmt.where(AuditLog.username == username)
        if start_time:
            stmt = stmt.where(AuditLog.timestamp >= start_time)
            count_stmt = count_stmt.where(AuditLog.timestamp >= start_time)
        if end_time:
            stmt = stmt.where(AuditLog.timestamp <= end_time)
            count_stmt = count_stmt.where(AuditLog.timestamp <= end_time)

        total = (await session.execute(count_stmt)).scalar() or 0
        stmt = (stmt.order_by(AuditLog.timestamp.desc())
                    .offset((page - 1) * page_size).limit(page_size))
        rows = (await session.execute(stmt)).scalars().all()

        return {
            "total": total,
            "page": page,
            "page_size": page_size,
            "logs": [
                {
                    "id": r.id,
                    "timestamp": r.timestamp.isoformat() if r.timestamp else None,
                    "trace_id": r.trace_id,
                    "username": r.username,
                    "tenant_id": r.tenant_id,
                    "action": r.action,
                    "resource": r.resource,
                    "detail": r.detail or {},
                    "ip": r.ip,
                    "result": r.result,
                    "duration_ms": r.duration_ms,
                }
                for r in rows
            ],
        }


async def list_audit_actions(tenant_id: str) -> List[str]:
    """列出该租户出现过的动作类型（供前端过滤器；含平台级的 NULL 租户记录）"""
    async with PlatformSessionLocal() as session:
        result = await session.execute(
            select(AuditLog.action)
            .where((AuditLog.tenant_id == tenant_id) | (AuditLog.tenant_id.is_(None)))
            .distinct()
        )
        return sorted(row[0] for row in result.all())
