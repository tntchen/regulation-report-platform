"""
制度文档数据模型
"""

from sqlalchemy import Column, String, Integer, DateTime, Boolean
from sqlalchemy.sql import func
from backend.database import Base


class RegulationDocument(Base):
    """制度文档模型"""
    __tablename__ = "regulation_documents"

    id = Column(String(32), primary_key=True, index=True)
    tenant_id = Column(String(32), nullable=False, index=True)
    filename = Column(String(200), nullable=False)
    doc_type = Column(String(50), nullable=False)  # 1104/EAST/利率报备/征信/通用安全合规/自定义
    file_path = Column(String(500))
    size = Column(Integer, default=0)
    status = Column(String(20), default="uploaded")  # uploaded/indexing/indexed/failed/disabled
    chunk_count = Column(Integer, default=0)

    # 向量信息
    vector_count = Column(Integer, default=0)
    indexed_at = Column(DateTime)
    index_duration_ms = Column(Integer)

    # 上传信息
    uploaded_by = Column(String(50))
    uploaded_at = Column(DateTime, server_default=func.now())

    # 版本控制
    version = Column(Integer, default=1)
    is_active = Column(Boolean, default=True)
