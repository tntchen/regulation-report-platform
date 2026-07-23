"""
API 公共依赖
租户上下文依赖注入（供各路由模块共用）
"""

from fastapi import HTTPException
from backend.core.tenant_context import TenantContext, PRESET_TENANTS


async def get_tenant(tenant_id: str = "T001"):
    """获取租户上下文（依赖注入）"""
    tenant_config = PRESET_TENANTS.get(tenant_id)
    if not tenant_config:
        raise HTTPException(status_code=404, detail="租户不存在")
    TenantContext.set_tenant(tenant_id, tenant_config)
    return tenant_config
