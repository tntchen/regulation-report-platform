"""
任务管理 API
"""

import uuid
from datetime import datetime
from fastapi import APIRouter, Depends
from backend.api.deps import get_tenant
from backend.core.orchestrator import TaskOrchestrator

router = APIRouter(tags=["任务管理"])


@router.post("/tenants/{tenant_id}/tasks")
async def create_task(tenant_id: str, task_data: dict, tenant: dict = Depends(get_tenant)):
    """创建报送任务"""
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
        "twin_compare_with": task_data.get("twin_compare_with", [])
    }

    # 执行任务
    orchestrator = TaskOrchestrator(tenant_id)
    result = await orchestrator.execute_task(task_context)

    return {
        "task_id": task_id,
        "status": result["status"],
        "progress": result["progress"],
        "stages": result["stages"],
        "outputs": result["outputs"],
        "duration_ms": result.get("duration_ms", 0)
    }


@router.get("/tenants/{tenant_id}/tasks")
async def list_tasks(tenant_id: str):
    """列出租户任务（按创建时间倒序，供任务大厅展示）"""
    from backend.services import task_service

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
async def get_task(tenant_id: str, task_id: str):
    """获取任务状态（含实时阶段明细）"""
    from backend.services import task_service
    from fastapi import HTTPException

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
