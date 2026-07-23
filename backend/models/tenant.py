"""
租户数据模型
"""

from sqlalchemy import Column, String, Integer, DateTime, JSON
from sqlalchemy.sql import func
from backend.database import Base


class Tenant(Base):
    """租户模型"""
    __tablename__ = "tenants"

    id = Column(String(32), primary_key=True, index=True)
    name = Column(String(100), nullable=False)
    code = Column(String(50), nullable=False, unique=True)
    status = Column(String(20), default="active")
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, onupdate=func.now())

    # 资源配置
    max_tasks_per_month = Column(Integer, default=1000)
    max_concurrent_tasks = Column(Integer, default=5)
    max_storage_gb = Column(Integer, default=100)

    # AI后端配置
    ai_backend = Column(JSON, default={})

    # 数据源配置
    data_sources = Column(JSON, default=[])

    # 制度库配置
    regulation_config = Column(JSON, default={})

    # Agent配置
    agent_config = Column(JSON, default={})
