"""
FastAPI入口
银行监管报送智能开发平台
启动方式: python -m backend.main 或 uvicorn backend.main:app
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager

from backend.config import settings
from backend.database import Base, platform_engine
from backend import models  # noqa: F401  确保所有模型注册到 Base.metadata
from backend.api import api_router


# 生命周期管理
@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动: 创建数据库表
    async with platform_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    # 关闭: 清理资源
    await platform_engine.dispose()


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    lifespan=lifespan
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 挂载业务路由
app.include_router(api_router)


# ============================================
# 健康检查
# ============================================
@app.get("/health")
async def health_check():
    return {"status": "ok", "version": settings.app_version}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=settings.host, port=settings.port)
