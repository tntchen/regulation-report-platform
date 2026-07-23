"""
租户管理 API
"""

from fastapi import APIRouter, HTTPException
from backend.core.tenant_context import PRESET_TENANTS

router = APIRouter(tags=["租户管理"])


@router.get("/tenants")
async def list_tenants():
    """列出所有租户"""
    return {
        "tenants": [
            {
                "id": t["id"],
                "name": t["name"],
                "code": t["code"],
                "status": "active"
            }
            for t in PRESET_TENANTS.values()
        ]
    }


@router.get("/tenants/{tenant_id}")
async def get_tenant_info(tenant_id: str):
    """获取租户信息"""
    tenant = PRESET_TENANTS.get(tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="租户不存在")
    return tenant
