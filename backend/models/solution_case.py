"""
历史方案库数据模型（范围 D）
任务 completed 时沉淀一条方案案例，供后续同包/同类型报表任务推荐参考
"""

from sqlalchemy import Column, String, DateTime, JSON
from sqlalchemy.sql import func
from backend.database import Base


class SolutionCase(Base):
    """方案案例：一次成功报送任务的终态摘要（映射终态 + 门禁结果 + 勾稽结果）"""
    __tablename__ = "solution_cases"

    id = Column(String(40), primary_key=True, index=True)   # "SC_xxxx"
    tenant_id = Column(String(32), nullable=False, index=True)
    report_pack_id = Column(String(32), nullable=False, index=True)  # 场景包 ID（缺省 G01）
    task_id = Column(String(40), nullable=False, unique=True, index=True)  # 一任务一案例

    # 终态摘要: {report_type, mapping: {...}, gate_result, reconciliation: {...}}
    summary = Column(JSON, default={})

    status = Column(String(20), default="completed")         # 案例状态（正常沉淀即 completed）
    created_by = Column(String(50))
    created_at = Column(DateTime, server_default=func.now())
