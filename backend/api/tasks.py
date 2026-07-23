"""
任务管理 API
L2-D4：创建任务异步化（落库 queued 秒回，worker 后台执行）+ 幂等键 + 任务取消
"""

import uuid
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Request
from backend.api.deps import get_tenant
from backend.services import task_service, audit_service

router = APIRouter(tags=["任务管理"])


@router.post("/tenants/{tenant_id}/tasks")
async def create_task(tenant_id: str, task_data: dict, request: Request,
                      tenant: dict = Depends(get_tenant)):
    """创建报送任务（异步）：落库 queued 立即返回，后台 worker 执行

    幂等：携带相同 client_request_id 的重复提交返回已有任务，不新建。
    """
    user = getattr(request.state, "user", None) or {}
    client_request_id = task_data.get("client_request_id")

    # 幂等检查：同租户 + 同用户 + 同 client_request_id → 返回已有任务
    if client_request_id:
        existing = await task_service.find_by_client_request_id(
            tenant_id, user.get("username"), client_request_id)
        if existing:
            return {
                "task_id": existing["task_id"],
                "status": existing["status"],
                "progress": existing["progress"],
                "idempotent": True,
                "message": "重复提交，返回已有任务",
            }

    task_id = f"TASK_{datetime.now().strftime('%Y%m%d')}_{uuid.uuid4().hex[:6]}"

    task_context = {
        "task_id": task_id,
        "tenant_id": tenant_id,
        "report_type": task_data.get("report_type", ""),
        "report_code": task_data.get("report_code", ""),
        "section": task_data.get("section", ""),
        "source_tables": task_data.get("source_tables", []),
        "target_table": task_data.get("target_table", ""),
        "output_mode": task_data.get("output_mode", "sql"),
        "dialect": task_data.get("dialect", "mysql"),
        "twin_compare_with": task_data.get("twin_compare_with", []),
        # HITL 映射工作台：场景包（不显式指定时 Agent 侧缺省 G01 兼容存量行为）+ 自动模式
        "report_pack_id": task_data.get("report_pack_id"),
        "auto_mode": bool(task_data.get("auto_mode", False)),
    }

    # 落库 queued，立即返回（worker 后台拾取执行）
    await task_service.create_queued_task(
        task_id, tenant_id, task_context,
        created_by=user.get("username"),
        client_request_id=client_request_id,
    )

    # 任务创建埋点
    await audit_service.write_audit(
        action="task.create",
        tenant_id=tenant_id,
        user=user or None,
        resource=task_id,
        detail={
            "report_type": task_context["report_type"],
            "report_code": task_context["report_code"],
            "target_table": task_context["target_table"],
            "report_pack_id": task_context["report_pack_id"],
            "auto_mode": task_context["auto_mode"],
            "client_request_id": client_request_id,
        },
        ip=request.client.host if request.client else None,
    )

    return {
        "task_id": task_id,
        "status": "queued",
        "progress": 0,
        "message": "任务已排队，后台执行中",
    }


@router.post("/tenants/{tenant_id}/tasks/{task_id}/cancel")
async def cancel_task(tenant_id: str, task_id: str, request: Request,
                      tenant: dict = Depends(get_tenant)):
    """取消任务：queued 直接 cancelled；executing 设置取消标记，阶段边界优雅终止"""
    state = await task_service.get_task_state(task_id)
    if not state or state.get("tenant_id") != tenant_id:
        raise HTTPException(status_code=404, detail="任务不存在")
    if state["status"] in ("completed", "failed", "cancelled"):
        raise HTTPException(status_code=409, detail=f"任务已终态({state['status']})，不可取消")

    ok = await task_service.set_cancel_requested(task_id)
    if not ok:
        raise HTTPException(status_code=409, detail="任务已终态，不可取消")

    latest = await task_service.get_task_state(task_id)

    # 任务取消埋点
    await audit_service.write_audit(
        action="task.cancel",
        tenant_id=tenant_id,
        user=getattr(request.state, "user", None),
        resource=task_id,
        detail={"previous_status": state["status"], "new_status": latest["status"]},
        ip=request.client.host if request.client else None,
    )

    return {
        "task_id": task_id,
        "status": latest["status"],
        "message": "任务已取消" if latest["status"] == "cancelled" else "取消请求已提交，任务将在当前阶段结束后停止",
    }


@router.get("/tenants/{tenant_id}/tasks")
async def list_tasks(tenant_id: str, tenant: dict = Depends(get_tenant)):
    """列出租户任务（按创建时间倒序，供任务大厅展示；需租户成员权限）"""
    states = await task_service.list_tasks(tenant_id)
    return {
        "total": len(states),
        "tasks": [
            {
                "task_id": s["task_id"],
                "name": s.get("name", ""),
                "status": s.get("status"),
                "current_stage": s.get("current_stage"),
                "progress": s.get("progress"),
                "retry_count": s.get("retry_count", 0),
                "duration_ms": s.get("duration_ms", 0),
                "created_at": s.get("created_at")
            }
            for s in states
        ]
    }


@router.get("/tenants/{tenant_id}/tasks/{task_id}")
async def get_task(tenant_id: str, task_id: str, tenant: dict = Depends(get_tenant)):
    """获取任务状态（含实时阶段明细；需租户成员权限）"""
    state = await task_service.get_task_state(task_id)
    if not state or state.get("tenant_id") != tenant_id:
        raise HTTPException(status_code=404, detail="任务不存在")

    return {
        "task_id": task_id,
        "tenant_id": tenant_id,
        "status": state.get("status"),
        "current_stage": state.get("current_stage"),
        "progress": state.get("progress"),
        "retry_count": state.get("retry_count", 0),
        "error": state.get("error"),
        "stages": state.get("stages", []),
        "outputs": state.get("outputs", {}),
        "duration_ms": state.get("duration_ms", 0)
    }
