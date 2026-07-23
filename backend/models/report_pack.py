"""
场景包数据模型
报表定义从代码变成数据：新增报表零代码（设计方案 §1.1）
"""

from sqlalchemy import Column, String, DateTime, Text, JSON
from sqlalchemy.sql import func
from backend.database import Base


class ReportPack(Base):
    """场景包模型：一张监管报表的完整定义（目标结构/候选源表/勾稽规则/检索关键词）"""
    __tablename__ = "report_packs"

    id = Column(String(32), primary_key=True, index=True)  # "G01" / "G11" / "EAST_JJ"
    report_name = Column(String(200), nullable=False)      # 报表名称
    report_type = Column(String(50), nullable=False)       # "1104" / "EAST" / ...
    target_table = Column(String(100), nullable=False)     # 目标表名

    # 目标表结构: [{field, data_type, required, caliber_text, expected_domain?}]
    target_schema = Column(JSON, default=[])
    # 候选源表: ["loan_contract", ...]
    source_tables = Column(JSON, default=[])
    # 勾稽规则: [{name, expression, tolerance}]
    reconciliation_rules = Column(JSON, default=[])
    # 关联陷阱关键词: ["逾期90天", "组合贷"]
    trap_refs = Column(JSON, default=[])
    # 制度检索关键词（Agent 1 检索用）
    regulation_keywords = Column(Text, default="")

    status = Column(String(20), default="active")          # active / draft / disabled
    created_by = Column(String(50))
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, onupdate=func.now())
