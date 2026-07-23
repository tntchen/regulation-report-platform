"""
字段映射数据模型（映射工作台 + 场景包，设计方案 §1.2）

记录单个任务内"目标字段 → 源字段"的映射结论与五通道证据，
状态机：ai_inferred → confirmed / modified / rejected / needs_etl（unmapped 为未映射终态前置）。
human-in-the-loop 确认动作由范围 C 的 API/编排层驱动，本模型仅承载数据。
"""

from sqlalchemy import Column, String, Float, DateTime, JSON, UniqueConstraint
from sqlalchemy.sql import func
from backend.database import Base


class FieldMapping(Base):
    """字段映射模型"""
    __tablename__ = "field_mappings"

    id = Column(String(32), primary_key=True, index=True)
    task_id = Column(String(32), nullable=False, index=True)        # 关联 tasks.id
    report_pack_id = Column(String(32), nullable=False, index=True)  # 关联场景包
    target_field = Column(String(100), nullable=False)               # 目标字段名
    source_table = Column(String(100))                               # 源表（未映射时为空）
    source_field = Column(String(100))                               # 源字段（未映射时为空）
    transform_rule = Column(String(500), default="DIRECT")           # DIRECT 或 SQL 表达式
    confidence = Column(Float, default=0.0)                          # 融合置信度 0-1
    evidence = Column(JSON, default={})                              # 五通道证据 {name/comment/profile/semantic/history}
    # ai_inferred / confirmed / modified / rejected / unmapped / needs_etl
    status = Column(String(20), default="ai_inferred", index=True)
    confirmed_by = Column(String(50))                                # 确认人（未确认为空）
    confirmed_at = Column(DateTime)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        # 契约：同一任务内一个目标字段只有一条映射记录
        UniqueConstraint("task_id", "target_field", name="uq_field_mapping_task_target"),
    )


class MappingStatus:
    """映射状态枚举（按设计方案 §1.2）"""
    AI_INFERRED = "ai_inferred"      # AI 推断，待人工确认
    CONFIRMED = "confirmed"          # 专家确认
    MODIFIED = "modified"            # 专家修改后确认
    REJECTED = "rejected"            # 专家拒绝 AI 推断
    UNMAPPED = "unmapped"            # 未能映射（置信度不足）
    NEEDS_ETL = "needs_etl"          # 需 ETL 加工（不阻断任务恢复）

    # 人工处理后的终态集合（confirm-all 校验用；unmapped/rejected 必须处理掉）
    FINAL_STATES = {CONFIRMED, MODIFIED, NEEDS_ETL}
