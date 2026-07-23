"""
created_by 透传修复验证（范围 C）
覆盖：
1) API 创建任务 → worker 执行完成 → SolutionCase.created_by == 实际登录用户名
2) tasks 表 created_by 列持久化，且编排器中间态 save 不冲刷
3) 无创建人信息时方案库沉淀回退 "system"

运行方式: python -m pytest tests/test_created_by.py -v
"""

import asyncio
import os
import tempfile
import time

# 在导入 app 之前切换到临时测试库 + 固定 JWT 密钥（与 test_task_async.py 同模式）
_tmpdir = tempfile.mkdtemp(prefix="rrp_test_created_by_")
os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{_tmpdir}/test_platform.db")
os.environ.setdefault("UPLOAD_DIR", f"{_tmpdir}/tenants")
os.environ.setdefault("DEMO_DB_PATH", f"{_tmpdir}/demo_biz.db")
os.environ.setdefault("TASK_WORK_DIR", f"{_tmpdir}/tasks")
os.environ.setdefault("LOG_DIR", f"{_tmpdir}/logs")
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-pytest")
os.environ.setdefault("DEBUG", "false")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.config import settings
from backend.main import app
from backend.database import PlatformSessionLocal
from backend.models.solution_case import SolutionCase
from backend.services import task_service
from backend.services.task_worker import TaskWorker

# 关闭应用内 worker 自动轮询：测试全部手动驱动调度，消除时序竞态
settings.task_worker_enabled = False


def drive_worker():
    """手动驱动一轮调度并等待执行完（替代后台轮询，保证测试确定性）"""
    async def _drive():
        w = TaskWorker()
        await w._schedule_once()
        if w._running:
            await asyncio.gather(*list(w._running), return_exceptions=True)
    asyncio.run(_drive())


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def login(client, username="admin", password="Admin@1234"):
    r = client.post("/v1/auth/login", json={"username": username, "password": password})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def wait_task(client, headers, task_id, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = client.get(f"/v1/tenants/T001/tasks/{task_id}", headers=headers)
        status = r.json()["status"]
        if status in ("completed", "failed", "cancelled"):
            return r.json()
        time.sleep(0.3)
    raise TimeoutError(f"任务 {task_id} 未在 {timeout}s 内到达终态")


def get_solution_case(task_id):
    async def _q():
        async with PlatformSessionLocal() as session:
            return (await session.execute(
                select(SolutionCase).where(SolutionCase.task_id == task_id)
            )).scalars().first()
    return asyncio.run(_q())


# ---------- 端到端：创建 → 完成 → 方案库溯源 ----------

def test_created_by_flows_to_solution_case(client):
    """创建任务的用户名最终落到 SolutionCase.created_by（不再写死 system）"""
    headers = login(client, "admin")
    r = client.post("/v1/tenants/T001/tasks", headers=headers, json={
        "report_type": "EAST", "report_code": "E_CB", "section": "个人住房贷款",
        "source_tables": ["loan_contract"], "target_table": "rpt_cb",
    })
    assert r.status_code == 200
    task_id = r.json()["task_id"]

    # tasks 表创建即携带 created_by
    async def _check_row():
        state = await task_service.get_task_state(task_id)
        return state
    state = asyncio.run(_check_row())
    assert state["created_by"] == "admin"

    drive_worker()
    final = wait_task(client, headers, task_id)
    assert final["status"] == "completed", f"任务未完成: {final.get('error')}"

    # 编排器多轮 save_task_state 后 created_by 未被冲刷
    state = asyncio.run(_check_row())
    assert state["created_by"] == "admin"

    # 方案库案例创建人为实际用户名
    case = get_solution_case(task_id)
    assert case is not None, "方案案例未沉淀"
    assert case.created_by == "admin"


# ---------- 兜底：无创建人信息回退 system ----------

def test_solution_case_fallback_system():
    """state 无任何 created_by 时沉淀回退 system（兼容历史任务/手工构造状态）"""
    from backend.services import solution_library

    async def _scenario():
        task_id = "TASK_CB_FALLBACK"
        await task_service.create_queued_task(
            task_id, "T001", {"report_type": "1104", "report_code": "CB_F"})
        state = await task_service.get_task_state(task_id)
        state["status"] = "completed"
        state.pop("created_by", None)
        await task_service.save_task_state(state)
        case_id = await solution_library.record_case_from_state(state)
        return case_id

    case_id = asyncio.run(_scenario())
    assert case_id is not None
    async def _q():
        async with PlatformSessionLocal() as session:
            return await session.get(SolutionCase, case_id)
    case = asyncio.run(_q())
    assert case.created_by == "system"


# ---------- 审计链路不受影响 ----------

def test_audit_chain_intact(client):
    """任务创建审计埋点仍正常记录（created_by 透传不影响审计）"""
    headers = login(client, "admin")
    r = client.post("/v1/tenants/T001/tasks", headers=headers, json={
        "report_type": "EAST", "report_code": "E_AUD", "section": "个人住房贷款",
        "source_tables": ["loan_contract"], "target_table": "rpt_aud",
    })
    assert r.status_code == 200
    data = client.get("/v1/tenants/T001/audit-logs?action=task.create&page_size=50",
                      headers=headers).json()
    assert any(l["resource"] == r.json()["task_id"] for l in data["logs"])

    # 清理：驱动 worker 跑完，避免泄漏
    drive_worker()
    wait_task(client, headers, r.json()["task_id"])
