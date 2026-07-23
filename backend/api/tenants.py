"""
租户管理 API
仅返回当前用户有权限的租户，避免租户信息枚举
"""

from fastapi import APIRouter, Depends
from backend.api.deps import get_current_user, get_tenant

router = APIRouter(tags=["租户管理"])


@router.get("/tenants")
async def list_tenants(current_user: dict = Depends(get_current_user)):
    """列出当前用户可访问的租户"""
    from backend.services import auth_service
    from backend.core.tenant_context import PRESET_TENANTS

    tenant_ids = await auth_service.get_user_tenants(current_user["id"])
    return {
        "tenants": [
            {
                "id": t["id"],
                "name": t["name"],
                "code": t["code"],
                "status": "active"
            }
            for t in (PRESET_TENANTS.get(tid) for tid in tenant_ids)
            if t
        ]
    }


@router.get("/tenants/{tenant_id}")
async def get_tenant_info(tenant_id: str, tenant: dict = Depends(get_tenant)):
    """获取租户信息（需成员权限）"""
    return tenant
