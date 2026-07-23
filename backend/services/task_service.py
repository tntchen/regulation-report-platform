"""
任务服务
任务状态 SQLite 持久化（M3 起替换内存版，服务重启后任务历史可查）。
使用 platform 配置库（SQLite）的 tasks 表，接口与内存版保持语义一致。
"""

from datetime import datetime
from typing import Dict, Any, Optional, List
from sqlalchemy import select
from backend.database import PlatformSessionLocal
from backend.models.task import Task


def _state_to_row(state: Dict[str, Any]) -> Dict[str, Any]:
    """把编排器任务状态映射为 tasks 表字段"""
    return {
        "id": state.get("task_id"),
        "tenant_id": state.get("tenant_id", ""),
        "task_type": state.get("task_type", "report"),
        "name": state.get("name") or f"报送任务 {state.get('task_id', '')}",
        "status": state.get("status", "created"),
        "current_stage": state.get("current_stage"),
        "progress": state.get("progress", 0),
        "report_config": state.get("report_config", {}),
        "stages": state.get("stages", []),
        "outputs": state.get("outputs", {}),
        "duration_ms": state.get("duration_ms"),
        "retry_count": state.get("retry_count", 0),
        "description": state.get("error"),  # 失败原因记入 description
        "completed_at": datetime.now() if state.get("status") in ("completed", "failed") else None,
    }


def _row_to_state(row: Task) -> Dict[str, Any]:
    """把 tasks 表记录还原为编排器任务状态结构"""
    return {
        "task_id": row.id,
        "tenant_id": row.tenant_id,
        "task_type": row.task_type,
        "name": row.name,
        "status": row.status,
        "current_stage": row.current_stage,
        "progress": row.progress,
        "stages": row.stages or [],
        "outputs": row.outputs or {},
        "duration_ms": row.duration_ms or 0,
        "retry_count": row.retry_count or 0,
        "error": row.description,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


async def save_task_state(state: Dict[str, Any]):
    """登记/更新任务状态（upsert）"""
    row_data = _state_to_row(state)
    async with PlatformSessionLocal() as session:
        existing = await session.get(Task, row_data["id"])
        if existing:
            for key, value in row_data.items():
                if key != "id":
                    setattr(existing, key, value)
        else:
            session.add(Task(**row_data))
        await session.commit()


async def get_task_state(task_id: str) -> Optional[Dict[str, Any]]:
    """查询任务状态"""
    async with PlatformSessionLocal() as session:
        row = await session.get(Task, task_id)
        return _row_to_state(row) if row else None


async def list_tasks(tenant_id: str = None) -> List[Dict[str, Any]]:
    """列出任务（可按租户过滤，按创建时间倒序）"""
    async with PlatformSessionLocal() as session:
        stmt = select(Task).order_by(Task.created_at.desc())
        if tenant_id:
            stmt = stmt.where(Task.tenant_id == tenant_id)
        result = await session.execute(stmt)
        return [_row_to_state(row) for row in result.scalars().all()]
