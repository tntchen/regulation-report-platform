"""
任务服务
任务状态的内存登记与查询（Demo 简化版，不持久化到数据库；
后续里程碑可替换为 SQLAlchemy 持久化实现，接口保持不变）
"""

from typing import Dict, Any, Optional

# 任务状态登记表: task_id -> state
_TASK_STORE: Dict[str, Dict[str, Any]] = {}


def save_task_state(state: Dict[str, Any]):
    """登记/更新任务状态"""
    task_id = state.get("task_id")
    if task_id:
        _TASK_STORE[task_id] = state


def get_task_state(task_id: str) -> Optional[Dict[str, Any]]:
    """查询任务状态"""
    return _TASK_STORE.get(task_id)


def list_tasks(tenant_id: str = None) -> list:
    """列出任务（可按租户过滤）"""
    states = list(_TASK_STORE.values())
    if tenant_id:
        states = [s for s in states if s.get("tenant_id") == tenant_id]
    return states
