"""
制度检索相关数据模型
索引日志 + 检索反馈
"""

from sqlalchemy import Column, String, Integer, DateTime, Boolean, Text
from sqlalchemy.sql import func
from backend.database import Base


class IndexLog(Base):
    """索引日志模型"""
    __tablename__ = "index_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    tenant_id = Column(String(32), nullable=False, index=True)
    operation = Column(String(50), nullable=False)  # upload/reindex/auto_index
    doc_id = Column(String(32))
    doc_name = Column(String(200))
    status = Column(String(20))  # success/failed
    message = Column(Text)
    duration_ms = Column(Integer)
    created_at = Column(DateTime, server_default=func.now())


class RetrievalFeedback(Base):
    """检索反馈模型"""
    __tablename__ = "retrieval_feedbacks"

    id = Column(Integer, primary_key=True, autoincrement=True)
    tenant_id = Column(String(32), nullable=False, index=True)
    query = Column(Text, nullable=False)
    result_rank = Column(Integer)
    is_accurate = Column(Boolean)
    comment = Column(Text)
    created_at = Column(DateTime, server_default=func.now())
