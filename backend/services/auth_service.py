"""
认证服务
登录校验、用户信息查询、演示用户种子初始化
"""

import uuid
from typing import Optional, Dict, Any, List

from sqlalchemy import select

from backend.database import PlatformSessionLocal
from backend.models.user import User, UserTenantBinding
from backend.utils.security import verify_password


# 预置演示用户（密码仅用于种子初始化，落库为 bcrypt 哈希）
DEMO_USERS = [
    {
        "username": "admin", "password": "Admin@1234",
        "display_name": "系统管理员", "role": "admin",
        "tenants": ["T001", "T002"],
    },
    {
        "username": "zhangsan", "password": "Zhangsan@1234",
        "display_name": "张三（零售信贷中心）", "role": "operator",
        "tenants": ["T001"],
    },
]


async def authenticate(username: str, password: str) -> Optional[User]:
    """校验账号密码，成功返回用户记录"""
    async with PlatformSessionLocal() as session:
        result = await session.execute(select(User).where(User.username == username))
        user = result.scalars().first()
        if not user or not user.is_active:
            return None
        if not verify_password(password, user.password_hash):
            return None
        return user


async def get_user(user_id: str) -> Optional[User]:
    """按 ID 查询用户"""
    async with PlatformSessionLocal() as session:
        return await session.get(User, user_id)


async def get_user_tenants(user_id: str) -> List[str]:
    """查询用户可访问的租户 ID 列表"""
    async with PlatformSessionLocal() as session:
        result = await session.execute(
            select(UserTenantBinding.tenant_id).where(UserTenantBinding.user_id == user_id)
        )
        return [row[0] for row in result.all()]


async def is_tenant_member(user_id: str, tenant_id: str) -> bool:
    """判定用户是否为租户成员（本期 RBAC 简化为成员判定）"""
    async with PlatformSessionLocal() as session:
        result = await session.execute(
            select(UserTenantBinding).where(
                UserTenantBinding.user_id == user_id,
                UserTenantBinding.tenant_id == tenant_id,
            )
        )
        return result.scalars().first() is not None


async def seed_demo_users():
    """初始化演示用户与租户绑定（幂等）"""
    from backend.utils.security import hash_password

    async with PlatformSessionLocal() as session:
        for demo in DEMO_USERS:
            result = await session.execute(
                select(User).where(User.username == demo["username"])
            )
            user = result.scalars().first()
            if not user:
                user = User(
                    id=uuid.uuid4().hex,
                    username=demo["username"],
                    password_hash=hash_password(demo["password"]),
                    display_name=demo["display_name"],
                    role=demo["role"],
                )
                session.add(user)
                await session.flush()  # 拿到 user.id

            for tenant_id in demo["tenants"]:
                result = await session.execute(
                    select(UserTenantBinding).where(
                        UserTenantBinding.user_id == user.id,
                        UserTenantBinding.tenant_id == tenant_id,
                    )
                )
                if not result.scalars().first():
                    session.add(UserTenantBinding(
                        user_id=user.id, tenant_id=tenant_id, role=demo["role"]
                    ))
        await session.commit()
