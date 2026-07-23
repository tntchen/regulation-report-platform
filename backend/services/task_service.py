"""
任务服务
任务状态 SQLite 持久化（M3 起替换内存版，服务重启后任务历史可查）。
L2-D4 增加：异步队列语义（queued 落库 / 取任务 / 断点 / 取消标记 / 幂等键）。
使用 platform 配置库（SQLite）的 tasks 表。
"""

from datetime import datetime
from typing import Dict, Any, Optional, List
from sqlalchemy import select, text, func
from backend.database import PlatformSessionLocal, platform_engine
from backend.models.task import Task

# 终态集合（不再被 worker 调度）
TERMINAL_STATUS = ("completed", "failed", "cancelled")


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
        "completed_at": datetime.now() if state.get("status") in TERMINAL_STATUS else None,
        "cancel_requested": 1 if state.get("cancel_requested") else 0,
        "checkpoint": state.get("checkpoint", {}),
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
        "report_config": row.report_config or {},
        "duration_ms": row.duration_ms or 0,
        "retry_count": row.retry_count or 0,
        "error": row.description,
        "created_by": row.created_by,
        "client_request_id": row.client_request_id,
        "cancel_requested": bool(row.cancel_requested),
        "checkpoint": row.checkpoint or {},
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


async def ensure_task_columns():
    """轻量列迁移：老库缺新列时 ALTER TABLE 补齐（Alembic 在 Day 10 引入前的过渡方案）"""
    new_columns = {
        "created_by": "VARCHAR(50)",
        "client_request_id": "VARCHAR(64)",
        "cancel_requested": "INTEGER DEFAULT 0",
        "checkpoint": "JSON",
    }
    async with platform_engine.begin() as conn:
        rows = (await conn.execute(text("PRAGMA table_info(tasks)"))).fetchall()
        existing = {r[1] for r in rows}
        for col, ddl in new_columns.items():
            if col not in existing:
                await conn.execute(text(f"ALTER TABLE tasks ADD COLUMN {col} {ddl}"))


async def save_task_state(state: Dict[str, Any]):
    """登记/更新任务状态（upsert）
    cancel_requested 为带外标记（API 设置）：state 未显式携带时不覆盖，防止被编排器状态冲刷"""
    row_data = _state_to_row(state)
    if "cancel_requested" not in state:
        row_data.pop("cancel_requested", None)
    # created_by 为创建期字段：仅 state 显式携带时才写，防止编排器中间态把已有值冲刷为 None
    if state.get("created_by"):
        row_data["created_by"] = state["created_by"]
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


# ============================================
# 异步队列语义（L2-D4）
# ============================================
async def create_queued_task(task_id: str, tenant_id: str, report_config: Dict[str, Any],
                             created_by: str = None,
                             client_request_id: str = None) -> Dict[str, Any]:
    """创建排队任务（落库即返回，由 worker 后台执行）"""
    name = f"{report_config.get('report_type', '')} {report_config.get('report_code', '')} 报送任务".strip()
    async with PlatformSessionLocal() as session:
        session.add(Task(
            id=task_id, tenant_id=tenant_id,
            task_type=report_config.get("report_type", "report"),
            name=name or f"报送任务 {task_id}",
            status="queued",
            report_config=report_config,
            created_by=created_by,
            client_request_id=client_request_id,
        ))
        await session.commit()
    return await get_task_state(task_id)


async def find_by_client_request_id(tenant_id: str, created_by: str,
                                    client_request_id: str) -> Optional[Dict[str, Any]]:
    """按幂等键查找已有任务（租户 + 创建人 + client_request_id）"""
    async with PlatformSessionLocal() as session:
        result = await session.execute(
            select(Task).where(
                Task.tenant_id == tenant_id,
                Task.created_by == created_by,
                Task.client_request_id == client_request_id,
            )
        )
        row = result.scalars().first()
        return _row_to_state(row) if row else None


async def fetch_queued_tasks(limit: int = 10) -> List[Dict[str, Any]]:
    """取排队中的任务（按创建时间正序，FIFO）"""
    async with PlatformSessionLocal() as session:
        result = await session.execute(
            select(Task).where(Task.status == "queued")
            .order_by(Task.created_at.asc()).limit(limit)
        )
        return [_row_to_state(row) for row in result.scalars().all()]


async def list_recoverable_tasks() -> List[Dict[str, Any]]:
    """列出需要恢复的任务：worker 启动时扫描
    - executing：进程死亡遗留，尝试断点续跑
    """
    async with PlatformSessionLocal() as session:
        result = await session.execute(
            select(Task).where(Task.status == "executing")
        )
        return [_row_to_state(row) for row in result.scalars().all()]


async def count_executing(tenant_id: Optional[str] = None) -> int:
    """统计执行中任务数（可按租户过滤，用于并发上限）"""
    async with PlatformSessionLocal() as session:
        stmt = select(func.count(Task.id)).where(Task.status == "executing")
        if tenant_id:
            stmt = stmt.where(Task.tenant_id == tenant_id)
        return (await session.execute(stmt)).scalar() or 0


async def set_cancel_requested(task_id: str) -> bool:
    """设置取消标记（executing 任务在阶段边界优雅终止）
    返回 False 表示任务已终态不可取消"""
    async with PlatformSessionLocal() as session:
        row = await session.get(Task, task_id)
        if not row or row.status in TERMINAL_STATUS:
            return False
        if row.status == "queued":
            # queued 直接取消
            row.status = "cancelled"
            row.completed_at = datetime.now()
            row.description = "排队中被用户取消"
        else:
            row.cancel_requested = 1
        await session.commit()
        return True


async def get_tenant_max_concurrent(tenant_id: str, default: int) -> int:
    """读取租户并发上限（tenants 表有记录则用其 max_concurrent_tasks，否则用全局默认）"""
    from backend.models.tenant import Tenant
    async with PlatformSessionLocal() as session:
        row = await session.get(Tenant, tenant_id)
        if row and row.max_concurrent_tasks:
            return row.max_concurrent_tasks
    return default
