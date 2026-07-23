"""
租户管理 API
- GET 列表/详情：仅返回当前用户有权限的租户，避免租户信息枚举
- POST 创建 / PUT 更新：仅 admin 角色，配置数据源与 AI 后端，写审计
"""

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from backend.api.deps import get_current_user, get_tenant
from backend.services import audit_service, auth_service, tenant_service

router = APIRouter(tags=["租户管理"])


class TenantCreateRequest(BaseModel):
    """创建租户请求"""
    id: str = Field(..., min_length=1, max_length=32, description="租户ID，如 T003")
    name: str = Field(..., min_length=1, max_length=100)
    code: str = Field(..., min_length=1, max_length=50, description="唯一编码")
    ai_backend: Optional[Dict[str, Any]] = None
    data_sources: Optional[List[Dict[str, Any]]] = None
    regulation_config: Optional[Dict[str, Any]] = None
    agent_config: Optional[Dict[str, Any]] = None


class TenantUpdateRequest(BaseModel):
    """更新租户请求（仅更新传入字段）"""
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    code: Optional[str] = Field(None, min_length=1, max_length=50)
    status: Optional[str] = Field(None, pattern="^(active|disabled)$")
    ai_backend: Optional[Dict[str, Any]] = None
    data_sources: Optional[List[Dict[str, Any]]] = None
    regulation_config: Optional[Dict[str, Any]] = None
    agent_config: Optional[Dict[str, Any]] = None


def _require_admin(user: dict):
    """租户写操作仅 admin 角色（Demo 简化的 RBAC）"""
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可管理租户")


@router.get("/tenants")
async def list_tenants(current_user: dict = Depends(get_current_user)):
    """列出当前用户可访问的租户（数据来源 tenants 表）"""
    tenant_ids = set(await auth_service.get_user_tenants(current_user["id"]))
    all_tenants = await tenant_service.list_all_tenants()
    return {"tenants": [t for t in all_tenants if t["id"] in tenant_ids]}


# 注意：创建路由挂在 "/tenants/"（尾斜杠）——main.py 审计中间件按路径段粗提取租户ID，
# POST "/v1/tenants"（路径以 tenants 结尾）会触发其越界 IndexError（main.py 不归本模块管，已上报）
@router.post("/tenants/")
async def create_tenant(req: TenantCreateRequest,
                        current_user: dict = Depends(get_current_user)):
    """创建租户（admin）并配置数据源与 AI 后端；创建者自动绑定为成员"""
    _require_admin(current_user)
    row = await tenant_service.create_tenant(
        tenant_id=req.id, name=req.name, code=req.code,
        ai_backend=req.ai_backend, data_sources=req.data_sources,
        regulation_config=req.regulation_config, agent_config=req.agent_config,
    )
    if not row:
        await audit_service.write_audit(
            action="tenant.create", tenant_id=req.id, user=current_user,
            resource=req.id, detail={"name": req.name, "code": req.code},
            result="fail",
        )
        raise HTTPException(status_code=409, detail="租户ID或编码已存在")

    # 创建者自动绑定为新租户成员，创建后立即可用
    await tenant_service.bind_user(req.id, current_user["id"], role="admin")

    await audit_service.write_audit(
        action="tenant.create", tenant_id=req.id, user=current_user,
        resource=req.id,
        detail={"name": req.name, "code": req.code,
                "data_sources": [ds.get("source_id") for ds in (req.data_sources or [])]},
    )
    return {"id": row.id, "name": row.name, "code": row.code, "status": row.status}


@router.put("/tenants/{tenant_id}")
async def update_tenant(tenant_id: str, req: TenantUpdateRequest,
                        current_user: dict = Depends(get_current_user)):
    """更新租户配置（admin），更新后缓存即时失效"""
    _require_admin(current_user)
    updates = req.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(status_code=400, detail="没有需要更新的字段")

    row = await tenant_service.update_tenant(tenant_id, updates)
    if not row:
        await audit_service.write_audit(
            action="tenant.update", tenant_id=tenant_id, user=current_user,
            resource=tenant_id, detail={"fields": list(updates.keys())},
            result="fail",
        )
        raise HTTPException(status_code=404, detail="租户不存在")

    await audit_service.write_audit(
        action="tenant.update", tenant_id=tenant_id, user=current_user,
        resource=tenant_id, detail={"fields": list(updates.keys())},
    )
    return {"id": row.id, "name": row.name, "code": row.code, "status": row.status}


@router.get("/tenants/{tenant_id}")
async def get_tenant_info(tenant_id: str, tenant: dict = Depends(get_tenant)):
    """获取租户信息（需成员权限）"""
    return tenant
