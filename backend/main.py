"""
FastAPI入口
银行监管报送智能开发平台
启动方式: python -m backend.main 或 uvicorn backend.main:app
"""

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from contextlib import asynccontextmanager
import time
import uuid

from backend.config import settings
from backend.database import Base, platform_engine
from backend import models  # noqa: F401  确保所有模型注册到 Base.metadata
from backend.api import api_router
from backend.utils.logging import setup_logging, get_logger, trace_id_ctx

logger = get_logger(__name__)


# 生命周期管理
@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动: 初始化全局日志 + 创建数据库表 + 初始化演示用户 + 校验密钥配置
    setup_logging()
    from backend.services.auth_service import seed_demo_users
    from backend.services import task_service
    from backend.services.task_worker import worker
    from backend.utils.security import get_jwt_secret
    get_jwt_secret()  # 非 debug 模式且未配置 SECRET_KEY 时在此报错
    async with platform_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await task_service.ensure_task_columns()  # 轻量列迁移（老库补新列）
    await seed_demo_users()
    # 启动任务 worker（恢复中断任务 + 轮询执行 queued 任务）
    if settings.task_worker_enabled:
        await worker.start()
    logger.info("平台启动完成 version=%s", settings.app_version)
    yield
    # 关闭: 停止 worker + 清理资源
    if settings.task_worker_enabled:
        await worker.stop()
    await platform_engine.dispose()


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    lifespan=lifespan
)

# CORS（允许源从配置读取，默认仅本地开发源；不使用 "*" + credentials 组合）
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.cors_origins.split(",") if o.strip()],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============================================
# trace_id + 审计中间件
# ============================================
@app.middleware("http")
async def trace_and_audit_middleware(request: Request, call_next):
    """每个请求生成 trace_id 贯穿日志；非 GET 写操作自动落审计"""
    from backend.services.audit_service import write_audit  # 延迟导入避免循环

    trace_id = uuid.uuid4().hex[:16]
    trace_id_ctx.set(trace_id)
    request.state.trace_id = trace_id

    start = time.time()
    response = await call_next(request)
    duration_ms = int((time.time() - start) * 1000)

    method = request.method
    path = request.url.path
    logger.info("%s %s -> %s (%dms)", method, path, response.status_code, duration_ms)

    # 写操作（POST/PUT/DELETE）自动审计；用户身份由 get_current_user 写入 request.state
    if method in ("POST", "PUT", "DELETE"):
        user = getattr(request.state, "user", None)
        # 从路径粗提取租户ID（/v1/tenants/{tid}/...）
        parts = path.split("/")
        tenant_id = parts[parts.index("tenants") + 1] if "tenants" in parts else None
        await write_audit(
            action="http.write",
            tenant_id=tenant_id,
            user=user,
            resource=f"{method} {path}",
            detail={"status_code": response.status_code},
            ip=request.client.host if request.client else None,
            result="success" if response.status_code < 400 else "fail",
            duration_ms=duration_ms,
        )

    response.headers["X-Trace-ID"] = trace_id
    return response


# ============================================
# 全局异常处理（5xx 带 trace_id）
# ============================================
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    trace_id = getattr(request.state, "trace_id", trace_id_ctx.get("-"))
    logger.error("未处理异常 %s %s: %s", request.method, request.url.path, exc, exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"detail": "服务器内部错误", "trace_id": trace_id},
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
