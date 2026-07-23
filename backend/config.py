"""
配置管理模块
支持从环境变量、.env文件加载配置
"""

from pydantic_settings import BaseSettings
from typing import Optional, List


class Settings(BaseSettings):
    """应用配置"""

    # 应用信息
    app_name: str = "regulation-report-platform"
    app_version: str = "2.0.0"
    debug: bool = True
    host: str = "0.0.0.0"
    port: int = 8080

    # 数据库配置
    database_url: str = "sqlite+aiosqlite:///./data/platform.db"

    # Redis配置
    redis_url: Optional[str] = None
    use_redis: bool = False

    # AI后端配置(主)
    ai_backend_provider: str = "kimi"
    ai_base_url: str = "http://10.0.1.100:8000/v1"
    ai_api_key: str = ""
    ai_model: str = "kimi-pro"
    ai_temperature: float = 0.3
    ai_max_tokens: int = 8192
    ai_timeout: int = 60

    # 离线Mock模式: true时强制使用内置MockAIAdapter，不依赖真实AI服务
    ai_mock_mode: bool = True

    # AI后端配置(备用)
    ai_backup_provider: str = "local"
    ai_backup_base_url: str = "http://localhost:8000/v1"
    ai_backup_api_key: str = ""
    ai_backup_model: str = "qwen-72b"

    # 向量库配置
    vector_store_type: str = "sqlite"      # 切片与向量存储：sqlite（租户独立 vectors.db）
    vector_dimension: int = 512            # 向量维度（与 embedding 模型一致：BGE-small-zh=512）
    embedding_model: str = "BAAI/bge-small-zh-v1.5"
    # Embedding 提供方: local=本地 sentence-transformers 语义模型(默认) /
    # remote=OpenAI 兼容 embedding 端点 / tfidf=字符 n-gram TF-IDF 兜底(无模型依赖)
    embedding_provider: str = "local"
    # remote provider 配置（缺省复用 AI 后端配置）
    embedding_remote_base_url: str = ""
    embedding_remote_api_key: str = ""
    embedding_remote_model: str = ""
    # 双通道融合检索权重与阈值
    retrieval_vector_weight: float = 0.7   # 向量余弦通道权重
    retrieval_text_weight: float = 0.3     # bigram 文本通道权重
    retrieval_threshold: float = 0.15      # 融合分召回阈值

    # 安全配置
    # JWT 密钥：必须从环境变量 SECRET_KEY 注入；debug 模式允许内置开发密钥兜底，
    # 非 debug 模式缺失时启动报错（见 utils/security.get_jwt_secret）
    secret_key: str = ""
    access_token_expire_minutes: int = 480  # token 有效期，默认 8 小时

    # CORS 允许源（逗号分隔，默认仅本地开发源；禁止 "*" + credentials 组合）
    cors_origins: str = "http://localhost:5173,http://localhost:7100,http://127.0.0.1:5173,http://127.0.0.1:7100"

    # 文件上传
    max_upload_size: int = 10 * 1024 * 1024  # 10MB
    upload_dir: str = "./data/tenants"

    # MCP配置
    mcp_database_readonly: bool = True
    mcp_database_whitelist: str = "loan_contract,customer_info,repay_plan,product_mapping"
    mcp_max_limit: int = 1000

    # 演示数据集(SQLite种子库，供Agent 4/5离线验证)
    demo_db_path: str = "./data/demo_biz.db"

    # 任务交付物工作目录
    task_work_dir: str = "./data/tasks"

    # 任务 worker（异步执行引擎）
    task_worker_enabled: bool = True        # 是否随应用启动内置 worker（外部队列替换时关闭）
    task_worker_max_concurrency: int = 2    # 全局并发执行上限
    task_worker_poll_interval: float = 0.5  # 轮询 queued 任务间隔（秒）

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


# 全局配置实例
settings = Settings()
