"""
审计日志 API
- GET /v1/tenants/{tid}/audit-logs: 分页查询（action/username/时间过滤）
- GET /v1/tenants/{tid}/audit-logs/actions: 动作类型清单（供前端过滤器）
"""

from typing import Optional

from fastapi import APIRouter, Depends, Query

from backend.api.deps import get_tenant
from backend.services import audit_service

router = APIRouter(prefix="/tenants/{tenant_id}/audit-logs", tags=["audit"])


@router.get("")
async def list_audit_logs(
    tenant: dict = Depends(get_tenant),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    action: Optional[str] = Query(None),
    username: Optional[str] = Query(None),
    start_time: Optional[str] = Query(None),
    end_time: Optional[str] = Query(None),
):
    """分页查询当前租户的审计日志"""
    return await audit_service.query_audit_logs(
        tenant_id=tenant["id"],
        action=action,
        username=username,
        start_time=start_time,
        end_time=end_time,
        page=page,
        page_size=page_size,
    )


@router.get("/actions")
async def list_actions(tenant: dict = Depends(get_tenant)):
    """列出当前租户出现过的审计动作类型（前端过滤器用）"""
    return {"actions": await audit_service.list_audit_actions(tenant["id"])}
