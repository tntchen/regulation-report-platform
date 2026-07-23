"""
租户上下文管理
实现多租户隔离的核心机制
"""

from contextvars import ContextVar
from typing import Optional, Dict, Any
from backend.config import settings

# 租户上下文变量
tenant_context: ContextVar[Dict[str, Any]] = ContextVar("tenant_context", default={})


class TenantContext:
    """租户上下文管理器"""

    @staticmethod
    def set_tenant(tenant_id: str, tenant_config: dict):
        """设置当前租户上下文"""
        tenant_context.set({
            "tenant_id": tenant_id,
            "config": tenant_config,
            "ai_backend": tenant_config.get("ai_backend", {}),
            "data_sources": tenant_config.get("data_sources", []),
            "regulation_config": tenant_config.get("regulation_config", {}),
            "agent_config": tenant_config.get("agent_config", {})
        })

    @staticmethod
    def get_current_tenant() -> Optional[Dict[str, Any]]:
        """获取当前租户上下文"""
        return tenant_context.get()

    @staticmethod
    def get_tenant_id() -> Optional[str]:
        """获取当前租户ID"""
        ctx = tenant_context.get()
        return ctx.get("tenant_id") if ctx else None

    @staticmethod
    def get_ai_backend() -> Dict[str, Any]:
        """获取当前租户AI后端配置"""
        ctx = tenant_context.get()
        return ctx.get("ai_backend", {}) if ctx else {}

    @staticmethod
    def get_data_sources() -> list:
        """获取当前租户数据源配置"""
        ctx = tenant_context.get()
        return ctx.get("data_sources", []) if ctx else []

    @staticmethod
    def get_regulation_config() -> Dict[str, Any]:
        """获取当前租户制度库配置"""
        ctx = tenant_context.get()
        return ctx.get("regulation_config", {}) if ctx else {}

    @staticmethod
    def get_agent_config() -> Dict[str, Any]:
        """获取当前租户Agent配置"""
        ctx = tenant_context.get()
        return ctx.get("agent_config", {}) if ctx else {}

    @staticmethod
    def clear():
        """清除租户上下文"""
        tenant_context.set({})


# 预置租户配置(演示用)
PRESET_TENANTS = {
    "T001": {
        "id": "T001",
        "name": "零售信贷中心",
        "code": "RETAIL_CREDIT",
        "ai_backend": {
            "provider": "kimi",
            "base_url": settings.ai_base_url,
            "api_key": settings.ai_api_key,
            "model": settings.ai_model,
            "temperature": settings.ai_temperature,
            "max_tokens": settings.ai_max_tokens
        },
        "data_sources": [
            {
                "source_id": "DS001",
                "source_name": "零售信贷主库",
                # sqlite_demo = 离线演示数据集（真实执行）；
                # 生产部署改为 mysql 并指向真实只读实例（见 scripts/seed_mysql.py）
                "db_type": "sqlite_demo",
                "readonly": True,
                "whitelist_tables": ["loan_contract", "customer_info", "repay_plan", "product_mapping"]
            }
        ],
        "regulation_config": {
            "doc_types": ["1104", "EAST", "利率报备", "征信", "通用安全合规"],
            "vector_db": {
                "provider": "faiss",
                "collection": "regulation_docs_t001"
            }
        },
        "agent_config": {
            "enabled_agents": ["regulation_parser", "codegen", "quality_gate", "test_verify", "digital_twin", "deploy"],
            "quality_gate_rules": {
                "block_on_critical": True,
                "warn_on_medium": 3
            }
        }
    },
    "T002": {
        "id": "T002",
        "name": "对公业务中心",
        "code": "CORPORATE",
        "ai_backend": {
            "provider": "local",
            "base_url": settings.ai_backup_base_url,
            "api_key": settings.ai_backup_api_key,
            "model": settings.ai_backup_model,
            "temperature": 0.3,
            "max_tokens": 4096
        },
        "data_sources": [
            {
                "source_id": "DS002",
                "source_name": "对公业务主库",
                # 与 T001 同一演示数据集（Demo 裁剪）；生产部署改为 mysql 独立 schema
                "db_type": "sqlite_demo",
                "readonly": True,
                "whitelist_tables": ["loan_contract"]
            }
        ],
        "regulation_config": {
            "doc_types": ["1104", "金融基础数据"],
            "vector_db": {
                "provider": "faiss",
                "collection": "regulation_docs_t002"
            }
        },
        "agent_config": {
            "enabled_agents": ["regulation_parser", "codegen", "quality_gate", "test_verify", "digital_twin", "deploy"],
            "quality_gate_rules": {
                "block_on_critical": True,
                "warn_on_medium": 3
            }
        }
    }
}
