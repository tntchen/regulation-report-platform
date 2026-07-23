"""
数据模型包
统一导出所有模型，保证 Base.metadata 能收集到全部表定义
"""

from backend.models.tenant import Tenant
from backend.models.task import Task
from backend.models.document import RegulationDocument
from backend.models.regulation import IndexLog, RetrievalFeedback
from backend.models.user import User, UserTenantBinding
from backend.models.audit_log import AuditLog
from backend.models.report_pack import ReportPack

__all__ = ["Tenant", "Task", "RegulationDocument", "IndexLog", "RetrievalFeedback",
           "User", "UserTenantBinding", "AuditLog", "ReportPack"]

# 映射工作台相关模型（范围 B 产出）：文件就绪后纳入 metadata 收集，未就绪时静默跳过
try:
    from backend.models.field_mapping import FieldMapping
    from backend.models.mapping_asset import MappingAsset
    __all__ += ["FieldMapping", "MappingAsset"]
except ImportError:
    pass
