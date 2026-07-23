"""
报送台账 + 截止期 API
路由前缀 /v1/tenants/{tenant_id}/ledger（由 main.py 统一注册挂载）。
- GET  列表：租户成员可读，status 懒计算 overdue
- POST generate：按 active 场景包批量生成当月台账（幂等），写审计 ledger.generate
- POST submit：报送完成，写审计 ledger.submit
- POST bind-task：绑定生成任务，pending → in_progress
"""

from fastapi import APIRouter, Depends, HTTPException, Request

from backend.api.deps import get_tenant, get_current_user
from backend.services import ledger_service, audit_service

# 独立导出，前缀由路由注册方统一指定
ledger_router = APIRouter(tags=["报送台账"])


@ledger_router.get("/tenants/{tenant_id}/ledger")
async def list_ledger(tenant_id: str, period: str = None,
                      tenant: dict = Depends(get_tenant)):
    """台账列表（租户成员可读）；period 可选过滤 YYYY-MM，entries 含 days_left"""
    if period is not None and not ledger_service.validate_period(period):
        raise HTTPException(status_code=422, detail=f"period 格式非法: {period!r}，应为 YYYY-MM")
    entries = await ledger_service.list_ledger(tenant_id, period=period)
    return {"entries": entries}


@ledger_router.post("/tenants/{tenant_id}/ledger/generate")
async def generate_ledger(tenant_id: str, payload: dict, request: Request,
                          tenant: dict = Depends(get_tenant),
                          current_user: dict = Depends(get_current_user)):
    """批量生成当月台账（幂等：已存在条目跳过），写审计 ledger.generate"""
    period = (payload or {}).get("period")
    if not period or not ledger_service.validate_period(period):
        raise HTTPException(status_code=422, detail=f"period 格式非法: {period!r}，应为 YYYY-MM")

    result = await ledger_service.generate_ledger(tenant_id, period)

    await audit_service.write_audit(
        action="ledger.generate",
        tenant_id=tenant_id,
        user=current_user,
        resource=period,
        detail={"created": result["created"], "skipped": result["skipped"]},
        ip=request.client.host if request.client else None,
    )

    entries = await ledger_service.list_ledger(tenant_id, period=period)
    return {**result, "entries": entries}


@ledger_router.post("/tenants/{tenant_id}/ledger/{entry_id}/submit")
async def submit_entry(tenant_id: str, entry_id: str, request: Request,
                       tenant: dict = Depends(get_tenant),
                       current_user: dict = Depends(get_current_user)):
    """报送完成（pending/in_progress/overdue → submitted；重复报送幂等），写审计 ledger.submit"""
    entry = await ledger_service.submit_entry(entry_id, tenant_id)
    if not entry:
        raise HTTPException(status_code=404, detail="台账条目不存在")

    await audit_service.write_audit(
        action="ledger.submit",
        tenant_id=tenant_id,
        user=current_user,
        resource=entry_id,
        detail={"report_pack_id": entry["report_pack_id"], "period": entry["period"],
                "report_name": entry["report_name"]},
        ip=request.client.host if request.client else None,
    )
    return entry


@ledger_router.post("/tenants/{tenant_id}/ledger/{entry_id}/bind-task")
async def bind_task(tenant_id: str, entry_id: str, payload: dict,
                    tenant: dict = Depends(get_tenant),
                    current_user: dict = Depends(get_current_user)):
    """绑定生成任务：任务须存在且同租户；pending 升级为 in_progress；已报送 409"""
    task_id = (payload or {}).get("task_id")
    if not task_id:
        raise HTTPException(status_code=422, detail="缺少必填字段: task_id")
    try:
        entry = await ledger_service.bind_task(entry_id, tenant_id, task_id)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    if not entry:
        raise HTTPException(status_code=404, detail="台账条目不存在")
    return entry
