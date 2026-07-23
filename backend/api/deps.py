"""
API 公共依赖
- get_current_user: Bearer token 解析（FastAPI 标准 HTTPBearer）
- get_tenant: 认证 + 租户成员校验（非成员 403）
"""

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from backend.core.tenant_context import TenantContext
from backend.services import auth_service, tenant_service
from backend.utils.security import decode_access_token, JWTError

# FastAPI 标准 Bearer 认证方案
bearer_scheme = HTTPBearer(auto_error=False)


async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
) -> dict:
    """解析 Bearer token，返回当前用户（未认证 401），并写入 request.state 供审计中间件使用"""
    if not credentials:
        raise HTTPException(status_code=401, detail="未认证：缺少 Bearer token",
                            headers={"WWW-Authenticate": "Bearer"})
    try:
        payload = decode_access_token(credentials.credentials)
    except JWTError:
        raise HTTPException(status_code=401, detail="token 无效或已过期",
                            headers={"WWW-Authenticate": "Bearer"})

    user = await auth_service.get_user(payload.get("sub", ""))
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="用户不存在或已停用")

    current = {
        "id": user.id,
        "username": user.username,
        "display_name": user.display_name,
        "role": user.role,
    }
    # 写入 request.state，审计中间件在请求结束后读取
    request.state.user = current
    return current


async def get_tenant(tenant_id: str = "T001",
                     current_user: dict = Depends(get_current_user)) -> dict:
    """获取租户上下文（认证 + 租户成员校验；配置从 tenants 表加载，带缓存）"""
    tenant_config = await tenant_service.get_tenant_config(tenant_id)
    if not tenant_config:
        raise HTTPException(status_code=404, detail="租户不存在")

    if not await auth_service.is_tenant_member(current_user["id"], tenant_id):
        raise HTTPException(status_code=403, detail="无权访问该租户")

    TenantContext.set_tenant(tenant_id, tenant_config)
    return tenant_config
