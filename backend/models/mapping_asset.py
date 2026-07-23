"""
历史映射资产数据模型（设计方案 §1.3）

专家确认后的映射沉淀为资产，下次同场景包任务的 history 通道直接命中（得分 1.0），
use_count 记录复用次数，用于评估资产质量与演示故事线"越用越准"。
"""

from sqlalchemy import Column, String, Integer, DateTime, UniqueConstraint
from sqlalchemy.sql import func
from backend.database import Base


class MappingAsset(Base):
    """历史映射资产模型"""
    __tablename__ = "mapping_assets"

    id = Column(String(32), primary_key=True, index=True)
    report_pack_id = Column(String(32), nullable=False, index=True)  # 场景包
    target_field = Column(String(100), nullable=False)
    source_table = Column(String(100), nullable=False)
    source_field = Column(String(100), nullable=False)
    transform_rule = Column(String(500), default="DIRECT")
    use_count = Column(Integer, default=0)                           # 复用次数
    last_confirmed_by = Column(String(50))
    last_confirmed_at = Column(DateTime)

    __table_args__ = (
        # 契约：同一(场景包, 目标字段, 源表, 源字段)组合唯一
        UniqueConstraint("report_pack_id", "target_field", "source_table", "source_field",
                         name="uq_mapping_asset_combo"),
    )
