"""
安全工具：密码哈希 + JWT
- 密码：passlib bcrypt（成熟库标准用法）
- JWT：python-jose HS256，过期时间可配置（默认 8 小时）
- secret_key：必须从环境变量 SECRET_KEY 注入；仅在 debug 模式下允许使用内置开发密钥，
  非 debug 模式缺失时启动报错（评审要求）
"""

from datetime import datetime, timedelta
from typing import Optional, Dict, Any

from jose import jwt, JWTError
from passlib.context import CryptContext

from backend.config import settings

# passlib 标准密码上下文
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

JWT_ALGORITHM = "HS256"
# debug 模式下的开发兜底密钥（生产必须配置 SECRET_KEY 环境变量）
_DEV_FALLBACK_SECRET = "dev-only-insecure-secret"


def get_jwt_secret() -> str:
    """获取 JWT 密钥：环境注入优先，非 debug 模式缺失直接报错"""
    if settings.secret_key:
        return settings.secret_key
    if settings.debug:
        return _DEV_FALLBACK_SECRET
    raise RuntimeError("SECRET_KEY 未配置：非 debug 模式必须通过环境变量注入密钥")


def hash_password(plain: str) -> str:
    """bcrypt 哈希密码"""
    return pwd_context.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    """校验密码"""
    return pwd_context.verify(plain, hashed)


def create_access_token(user_id: str, username: str,
                        expires_minutes: Optional[int] = None) -> Dict[str, Any]:
    """签发 JWT access token，返回 token 与过期秒数"""
    expire_minutes = expires_minutes or settings.access_token_expire_minutes
    expire = datetime.utcnow() + timedelta(minutes=expire_minutes)
    payload = {
        "sub": user_id,
        "username": username,
        "exp": expire,
        "iat": datetime.utcnow(),
    }
    token = jwt.encode(payload, get_jwt_secret(), algorithm=JWT_ALGORITHM)
    return {"access_token": token, "expires_in": expire_minutes * 60}


def decode_access_token(token: str) -> Dict[str, Any]:
    """解析 JWT，失败（过期/伪造）抛 JWTError"""
    return jwt.decode(token, get_jwt_secret(), algorithms=[JWT_ALGORITHM])


__all__ = [
    "hash_password", "verify_password", "create_access_token",
    "decode_access_token", "JWTError", "get_jwt_secret",
]
