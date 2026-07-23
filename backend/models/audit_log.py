"""
审计日志数据模型
记录 who / when / tenant / action / resource / detail / ip / result
"""

from sqlalchemy import Column, String, Integer, DateTime, JSON
from sqlalchemy.sql import func
from backend.database import Base


class AuditLog(Base):
    """审计日志模型"""
    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    timestamp = Column(DateTime, server_default=func.now(), index=True)
    trace_id = Column(String(36), index=True)          # 请求级追踪 ID，贯穿日志与审计
    user_id = Column(String(32), index=True)           # 操作人（匿名时为 None）
    username = Column(String(50), index=True)
    tenant_id = Column(String(32), index=True)
    action = Column(String(50), nullable=False, index=True)  # 如 task.create / document.upload / auth.login
    resource = Column(String(200))                     # 操作对象（任务ID/文档名/接口路径等）
    detail = Column(JSON, default={})                  # 丰富上下文（禁止写入密码/token/敏感数据）
    ip = Column(String(50))
    result = Column(String(10), default="success")     # success / fail
    duration_ms = Column(Integer)
