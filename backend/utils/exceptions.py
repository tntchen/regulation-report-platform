"""
平台自定义异常定义
"""


class PlatformError(Exception):
    """平台基础异常"""
    pass


class TenantNotFoundError(PlatformError):
    """租户不存在"""
    pass


class MCPPermissionError(PlatformError):
    """MCP 权限不足（如只读约束被违反）"""
    pass


class QualityGateBlockedError(PlatformError):
    """质量门禁阻断（超过最大重试次数）"""
    pass
