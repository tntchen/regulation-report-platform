"""
API 路由包
统一挂载各模块路由
"""

from fastapi import APIRouter

from backend.api import tenants, tasks, regulations, mcp, auth

# 汇总路由，统一加 /v1 前缀
api_router = APIRouter(prefix="/v1")
api_router.include_router(auth.router)
api_router.include_router(tenants.router)
api_router.include_router(tasks.router)
api_router.include_router(regulations.router)
api_router.include_router(mcp.router)
