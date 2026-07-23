"""
用户与租户绑定数据模型
"""

from sqlalchemy import Column, String, Integer, DateTime, Boolean, UniqueConstraint
from sqlalchemy.sql import func
from backend.database import Base


class User(Base):
    """用户模型"""
    __tablename__ = "users"

    id = Column(String(32), primary_key=True, index=True)
    username = Column(String(50), nullable=False, unique=True, index=True)
    password_hash = Column(String(128), nullable=False)  # bcrypt 哈希，禁止明文
    display_name = Column(String(100), nullable=False)
    role = Column(String(20), default="operator")  # admin/operator/viewer（本期仅预留）
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, server_default=func.now())


class UserTenantBinding(Base):
    """用户-租户多对多绑定（成员关系即访问授权）"""
    __tablename__ = "user_tenant_bindings"
    __table_args__ = (UniqueConstraint("user_id", "tenant_id", name="uq_user_tenant"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String(32), nullable=False, index=True)
    tenant_id = Column(String(32), nullable=False, index=True)
    role = Column(String(20), default="operator")  # 租户内角色（本期仅预留）
    created_at = Column(DateTime, server_default=func.now())
