"""
任务数据模型
"""

from sqlalchemy import Column, String, Integer, DateTime, Text, JSON
from sqlalchemy.sql import func
from backend.database import Base


class Task(Base):
    """任务模型"""
    __tablename__ = "tasks"

    id = Column(String(32), primary_key=True, index=True)
    tenant_id = Column(String(32), nullable=False, index=True)
    task_type = Column(String(50), nullable=False)
    name = Column(String(200), nullable=False)
    description = Column(Text)
    status = Column(String(20), default="created")  # created/parsing/executing/validating/deploying/completed/failed
    current_stage = Column(String(50))
    progress = Column(Integer, default=0)

    # 任务配置
    report_config = Column(JSON, default={})
    data_source = Column(JSON, default={})
    output_config = Column(JSON, default={})
    quality_gate = Column(JSON, default={})

    # 执行结果
    stages = Column(JSON, default=[])
    outputs = Column(JSON, default={})

    # 耗时统计
    created_at = Column(DateTime, server_default=func.now())
    started_at = Column(DateTime)
    completed_at = Column(DateTime)
    duration_ms = Column(Integer)

    # 重试次数
    retry_count = Column(Integer, default=0)
