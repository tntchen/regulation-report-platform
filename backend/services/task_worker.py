"""
任务执行 Worker（L2-D4 任务异步化）
单进程 asyncio worker：随 FastAPI 生命周期启停，轮询 queued 任务后台执行。

设计要点：
- 与请求处理解耦：API 只负责落库 queued，worker 独立调度执行；
  worker 仅依赖 task_service 的队列语义接口，未来可平滑替换为外部队列
  （Celery/Redis 等）——届时关闭 task_worker_enabled，由外部消费者复用 run_task()。
- 并发上限：全局信号量（task_worker_max_concurrency）+ 租户级上限
  （tenants 表 max_concurrent_tasks，无记录时回退全局默认）。
- 断点恢复：启动时扫描 executing 遗留任务（进程死亡），从 checkpoint 续跑。
- 取消：执行中任务在阶段边界检查 cancel_requested 标记优雅终止。
"""

import asyncio
from typing import Dict, Any, Optional

from backend.config import settings
from backend.services import task_service
from backend.utils.logging import get_logger

logger = get_logger(__name__)


async def run_task(task_state: Dict[str, Any], resume: bool = False) -> Dict[str, Any]:
    """执行单个任务（worker 与外部队列共用的执行入口）

    task_state: tasks 表还原的状态（含 report_config / checkpoint）
    resume: True 表示断点续跑（进程重启恢复）
    """
    from backend.core.orchestrator import TaskOrchestrator  # 延迟导入避免循环

    task_id = task_state["task_id"]
    tenant_id = task_state["tenant_id"]
    task_context = dict(task_state.get("report_config") or {})
    task_context["task_id"] = task_id

    async def should_cancel() -> bool:
        """阶段边界取消检查：读库取最新取消标记"""
        latest = await task_service.get_task_state(task_id)
        return bool(latest and latest.get("cancel_requested"))

    orchestrator = TaskOrchestrator(tenant_id)
    try:
        if resume:
            logger.info("断点续跑任务 %s（从 %s 继续）",
                        task_id, (task_state.get("checkpoint") or {}).get("next"))
            return await orchestrator.execute_task(
                task_context, resume_state=task_state, should_cancel=should_cancel)
        return await orchestrator.execute_task(task_context, should_cancel=should_cancel)
    except Exception as e:
        # worker 兜底：编排器未捕获的异常不能让 worker 循环崩溃
        logger.error("任务 %s 执行异常: %s", task_id, e, exc_info=True)
        task_state["status"] = "failed"
        task_state["error"] = f"worker 执行异常: {e}"
        await task_service.save_task_state(task_state)
        return task_state


class TaskWorker:
    """内置异步任务 worker（随应用生命周期启停）"""

    def __init__(self):
        self._task: Optional[asyncio.Task] = None
        self._stopped = asyncio.Event()
        self._semaphore = asyncio.Semaphore(settings.task_worker_max_concurrency)
        self._running: set = set()  # 执行中的 asyncio.Task，用于优雅停止

    async def start(self):
        """启动 worker：先恢复遗留任务，再进入轮询循环"""
        self._stopped.clear()
        await self._recover_interrupted_tasks()
        self._task = asyncio.create_task(self._loop(), name="task-worker")
        logger.info("任务 worker 已启动（全局并发上限 %d）", settings.task_worker_max_concurrency)

    async def stop(self):
        """停止 worker：退出轮询，等待执行中任务到达阶段边界"""
        self._stopped.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        # 等待执行中任务完成当前阶段（它们会在下一阶段边界看到 stop 不属于取消，
        # 这里直接取消 asyncio 任务；断点已落库，下次启动可续跑）
        for t in list(self._running):
            t.cancel()
        if self._running:
            await asyncio.gather(*self._running, return_exceptions=True)
        logger.info("任务 worker 已停止")

    # ============================================
    # 恢复与调度
    # ============================================
    async def _recover_interrupted_tasks(self):
        """启动恢复：扫描 executing 遗留任务（进程死亡），标记回 queued 等待调度续跑。
        queued 任务无需处理，轮询循环会自然拾取。"""
        recoverable = await task_service.list_recoverable_tasks()
        for state in recoverable:
            checkpoint = state.get("checkpoint") or {}
            if state.get("cancel_requested"):
                # 死亡前已被请求取消：直接终结
                state["status"] = "cancelled"
                state["error"] = "任务被用户取消（进程中断后确认）"
                await task_service.save_task_state(state)
                logger.info("遗留任务 %s 已被请求取消，标记 cancelled", state["task_id"])
            elif checkpoint.get("next"):
                # 有可用断点：回退为 queued，由调度器续跑（断点在 run_task 时生效）
                state["status"] = "queued"
                await task_service.save_task_state(state)
                logger.info("遗留任务 %s 恢复为 queued，将从 %s 续跑",
                            state["task_id"], checkpoint["next"])
            else:
                # 无断点（刚开始执行就死亡）：从头重跑是安全的（各 Agent 产出幂等覆盖）
                state["status"] = "queued"
                state["stages"] = []
                state["outputs"] = {}
                state["progress"] = 0
                state["checkpoint"] = {"completed": [], "next": ["regulation_parser"]}
                await task_service.save_task_state(state)
                logger.info("遗留任务 %s 无断点，从头重跑", state["task_id"])

    async def _loop(self):
        """轮询循环：取 queued 任务，按并发上限调度执行"""
        while not self._stopped.is_set():
            try:
                await self._schedule_once()
            except Exception as e:
                logger.error("worker 调度异常: %s", e, exc_info=True)
            await asyncio.sleep(settings.task_worker_poll_interval)

    async def _schedule_once(self):
        """单轮调度：有空闲容量时拾取 queued 任务"""
        # 全局容量检查
        executing_total = await task_service.count_executing()
        capacity = settings.task_worker_max_concurrency - executing_total
        if capacity <= 0:
            return

        queued = await task_service.fetch_queued_tasks(limit=capacity)
        for task_state in queued:
            # 租户级并发上限
            tenant_limit = await task_service.get_tenant_max_concurrent(
                task_state["tenant_id"], settings.task_worker_max_concurrency)
            tenant_executing = await task_service.count_executing(task_state["tenant_id"])
            if tenant_executing >= tenant_limit:
                continue

            # 抢占标记 executing（SQLite 单写，Demo 深度足够）
            resume = bool((task_state.get("checkpoint") or {}).get("next")
                          and task_state.get("stages"))
            task_state["status"] = "executing"
            await task_service.save_task_state(task_state)

            t = asyncio.create_task(self._run_guarded(task_state, resume))
            self._running.add(t)
            t.add_done_callback(self._running.discard)

    async def _run_guarded(self, task_state: Dict[str, Any], resume: bool):
        """信号量保护的任务执行"""
        async with self._semaphore:
            await run_task(task_state, resume=resume)


# 全局 worker 单例（main.py lifespan 管理）
worker = TaskWorker()
