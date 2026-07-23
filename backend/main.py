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
        # 从路径粗提取租户ID（/v1/tenants/{tid}/...）；/v1/tenants 集合路径无 tid 时取 None
        parts = path.split("/")
        tenant_id = parts[parts.index("tenants") + 1] if "tenants" in parts and parts.index("tenants") + 1 < len(parts) else None
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

# 映射工作台 + 场景包路由（范围C统一注册）
from backend.api import mappings as mappings_api  # noqa: E402
app.include_router(mappings_api.router, prefix="/v1")
try:
    # A 的场景包路由（并行开发期间可能尚未就绪，兜底跳过不影响主应用）
    from backend.api.report_packs import report_packs_router  # noqa: E402
    app.include_router(report_packs_router, prefix="/v1")
except Exception as _e:
    logger.warning("report_packs 路由未就绪，跳过注册: %s", _e)
try:
    # 制度版本diff + 新旧逻辑回归路由（范围C；并行开发期间兜底跳过不影响主应用）
    from backend.api.diff import diff_router  # noqa: E402
    app.include_router(diff_router, prefix="/v1")
except Exception as _e:
    logger.warning("diff 路由未就绪，跳过注册: %s", _e)


# ============================================
# 健康检查（深度版）
# status 分级: ok=全部正常 / degraded=非核心组件异常 / down=数据库不可用
# 响应保留 status/version 字段，向后兼容旧调用方
# ============================================
@app.get("/health")
async def health_check():
    import os
    from sqlalchemy import text

    checks: dict = {}

    # 1) 平台库可写探测（临时表写/删，验证可写而不只是连通）
    try:
        async with platform_engine.begin() as conn:
            await conn.execute(text("CREATE TEMP TABLE IF NOT EXISTS _health_probe (id INTEGER)"))
            await conn.execute(text("INSERT INTO _health_probe VALUES (1)"))
            await conn.execute(text("DELETE FROM _health_probe"))
        checks["database"] = "ok"
    except Exception as exc:
        logger.error("健康检查: 平台库可写探测失败: %s", exc)
        checks["database"] = "fail"

    # 2) 租户向量目录可用（upload_dir 存在且可写，向量库按租户落在其下）
    try:
        os.makedirs(settings.upload_dir, exist_ok=True)
        probe = os.path.join(settings.upload_dir, ".health_probe")
        with open(probe, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(probe)
        checks["vector_dir"] = "ok"
    except Exception as exc:
        logger.error("健康检查: 向量目录探测失败: %s", exc)
        checks["vector_dir"] = "fail"

    # 3) AI 后端连通性（mock 模式显式标注，不假装真实连通）
    if settings.ai_mock_mode:
        checks["ai"] = "mock"
    else:
        try:
            import httpx
            async with httpx.AsyncClient(timeout=3.0) as client:
                resp = await client.get(
                    f"{settings.ai_base_url}/models",
                    headers={"Authorization": f"Bearer {settings.ai_api_key}"},
                )
            checks["ai"] = "ok" if resp.status_code < 500 else "fail"
        except Exception as exc:
            logger.warning("健康检查: AI 后端连通探测失败: %s", exc)
            checks["ai"] = "fail"

    # 分级：库挂=down；其余任一 fail=degraded；mock 视为可用（Demo 合法状态）
    if checks["database"] == "fail":
        status = "down"
    elif "fail" in checks.values():
        status = "degraded"
    else:
        status = "ok"

    return {"status": status, "version": settings.app_version, "checks": checks}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=settings.host, port=settings.port)
