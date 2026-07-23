"""
认证 API
POST /v1/auth/login  账号密码登录，签发 JWT（匿名可访问）
GET  /v1/auth/me     当前用户信息 + 可访问租户列表
"""

from fastapi import APIRouter, Depends, HTTPException, Request

from backend.api.deps import get_current_user
from backend.core.tenant_context import PRESET_TENANTS
from backend.services import auth_service, audit_service
from backend.utils.security import create_access_token

router = APIRouter(prefix="/auth", tags=["认证"])


@router.post("/login")
async def login(payload: dict, request: Request):
    """登录：账号密码 → JWT access token"""
    username = payload.get("username", "")
    password = payload.get("password", "")
    ip = request.client.host if request.client else None

    user = await auth_service.authenticate(username, password)
    if not user:
        # 登录失败埋点（detail 只记用户名，绝不记密码）
        await audit_service.write_audit(
            action="auth.login",
            user={"username": username},
            resource="POST /v1/auth/login",
            detail={"username": username, "reason": "用户名或密码错误"},
            ip=ip,
            result="fail",
        )
        raise HTTPException(status_code=401, detail="用户名或密码错误")

    token = create_access_token(user.id, user.username)
    # 登录成功埋点
    await audit_service.write_audit(
        action="auth.login",
        user={"id": user.id, "username": user.username},
        resource="POST /v1/auth/login",
        detail={"username": username},
        ip=ip,
    )
    return {
        "access_token": token["access_token"],
        "token_type": "bearer",
        "expires_in": token["expires_in"],
        "user": {
            "username": user.username,
            "display_name": user.display_name,
            "role": user.role,
        },
    }


@router.get("/me")
async def me(current_user: dict = Depends(get_current_user)):
    """当前用户信息 + 可访问租户列表"""
    tenant_ids = await auth_service.get_user_tenants(current_user["id"])
    tenants = [
        {
            "id": t["id"],
            "name": t["name"],
            "code": t["code"],
            "status": "active",
        }
        for t in (PRESET_TENANTS.get(tid) for tid in tenant_ids)
        if t
    ]
    return {
        "user": current_user,
        "tenants": tenants,
    }
