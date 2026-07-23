"""
数据库连接管理
支持SQLite(平台配置) + MySQL(租户业务数据)
"""

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import declarative_base
from sqlalchemy.pool import NullPool
from backend.config import settings

import os

# 确保 SQLite 平台库所在目录存在（数据库文件路径为相对路径时按当前工作目录解析）
if settings.database_url.startswith("sqlite"):
    db_file = settings.database_url.split("///")[-1]
    if db_file and db_file != ":memory:":
        os.makedirs(os.path.dirname(os.path.abspath(db_file)), exist_ok=True)

# 平台配置库(SQLite)
platform_engine = create_async_engine(
    settings.database_url,
    echo=settings.debug,
    poolclass=NullPool  # SQLite用NullPool避免并发问题
)

PlatformSessionLocal = async_sessionmaker(
    platform_engine,
    class_=AsyncSession,
    expire_on_commit=False
)

# 租户业务数据库连接池(动态创建)
tenant_engines = {}

Base = declarative_base()


async def get_platform_db():
    """获取平台配置库会话"""
    async with PlatformSessionLocal() as session:
        yield session


def get_tenant_engine(db_url: str):
    """获取或创建租户数据库引擎"""
    if db_url not in tenant_engines:
        tenant_engines[db_url] = create_async_engine(
            db_url,
            echo=False,
            pool_size=5,
            max_overflow=10
        )
    return tenant_engines[db_url]


async def get_tenant_db(tenant_id: str, db_config: dict):
    """获取租户业务数据库会话"""
    # 构建连接URL
    db_type = db_config.get("db_type", "mysql")
    host = db_config.get("host", "localhost")
    port = db_config.get("port", 3306)
    database = db_config.get("database", "")
    username = db_config.get("username", "")
    password = db_config.get("password", "")

    if db_type == "mysql":
        db_url = f"mysql+aiomysql://{username}:{password}@{host}:{port}/{database}"
    elif db_type == "postgresql":
        db_url = f"postgresql+asyncpg://{username}:{password}@{host}:{port}/{database}"
    else:
        raise ValueError(f"不支持的数据库类型: {db_type}")

    engine = get_tenant_engine(db_url)
    SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with SessionLocal() as session:
        yield session
