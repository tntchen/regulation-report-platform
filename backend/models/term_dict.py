"""
业务术语词典模型（范围 E）

把"业务口径里的俗称/别名 → 关联字段名 hints"沉淀为数据，
供映射引擎 name 通道作为子信号加分使用（词典缺失时静默降级）。

tenant_id 为空表示全局共享词条；租户级词条与全局词条叠加生效。
"""

from sqlalchemy import Column, String, DateTime, JSON, UniqueConstraint
from sqlalchemy.sql import func
from backend.database import Base


class TermDict(Base):
    """业务术语词条"""
    __tablename__ = "term_dicts"

    id = Column(String(32), primary_key=True, index=True)
    tenant_id = Column(String(32), index=True)          # 空 = 全局词条
    term = Column(String(100), nullable=False)           # 术语本名，如 "按揭"
    aliases = Column(JSON, default=[])                   # 别名列表，如 ["房贷","个人住房贷款"]
    field_hints = Column(JSON, default=[])               # 关联字段名 hints，如 ["principal_balance","loan_balance"]
    category = Column(String(50))                        # 分类：产品/余额/利率/质量/标识 等
    created_by = Column(String(50))
    created_at = Column(DateTime, server_default=func.now())

    __table_args__ = (
        # 幂等约束：同一租户（含全局）下术语名唯一
        UniqueConstraint("tenant_id", "term", name="uq_term_dict_tenant_term"),
    )
