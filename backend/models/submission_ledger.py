"""
报送台账数据模型
每张监管报表按月（period）生成一条台账记录，跟踪截止期与报送状态。
状态机：pending → in_progress（绑定任务）→ submitted（报送完成）；
当前时间超 deadline 且未 submitted → overdue（查询时懒计算，不落库）。
"""

from sqlalchemy import Column, String, DateTime, UniqueConstraint
from sqlalchemy.sql import func
from backend.database import Base


class SubmissionLedger(Base):
    """报送台账条目：(tenant_id, report_pack_id, period) 唯一"""
    __tablename__ = "submission_ledger"
    __table_args__ = (
        UniqueConstraint("tenant_id", "report_pack_id", "period",
                         name="uq_ledger_tenant_pack_period"),
    )

    id = Column(String(32), primary_key=True, index=True)
    tenant_id = Column(String(32), nullable=False, index=True)
    report_pack_id = Column(String(32), nullable=False)     # 关联场景包
    report_name = Column(String(200), nullable=False)       # 冗余报表名，台账列表直出
    period = Column(String(7), nullable=False)              # YYYY-MM（月度报送）
    deadline = Column(DateTime, nullable=False)             # 报送截止期
    status = Column(String(20), default="pending")          # pending/in_progress/submitted（overdue 懒计算）
    task_id = Column(String(32))                            # 绑定的生成任务（可空）
    submitted_at = Column(DateTime)                         # 报送完成时间（可空）
    created_at = Column(DateTime, server_default=func.now())
