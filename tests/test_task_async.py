"""
任务异步化 + 断点恢复集成测试（L2-D4）
覆盖：创建秒回 queued、worker 后台跑完、kill 重启断点续跑、幂等键、取消（queued/executing）、并发上限

运行方式: python -m pytest tests/test_task_async.py -v
"""

import asyncio
import os
import tempfile
import time

# 在导入 app 之前切换到临时测试库 + 固定 JWT 密钥（与 test_auth.py 同模式）
_tmpdir = tempfile.mkdtemp(prefix="rrp_test_async_")
os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{_tmpdir}/test_platform.db")
os.environ.setdefault("UPLOAD_DIR", f"{_tmpdir}/tenants")
os.environ.setdefault("DEMO_DB_PATH", f"{_tmpdir}/demo_biz.db")
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-pytest")
os.environ.setdefault("DEBUG", "false")

import pytest
from fastapi.testclient import TestClient

from backend.config import settings
from backend.main import app
from backend.services import task_service
from backend.services.task_worker import TaskWorker, run_task

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
    """TestClient 上下文触发 lifespan（建表 + 种子用户 + worker 启动）"""
    with TestClient(app) as c:
        yield c


def login(client, username="admin", password="Admin@1234"):
    r = client.post("/v1/auth/login", json={"username": username, "password": password})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def wait_task(client, headers, task_id, timeout=30):
    """轮询任务直到终态"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = client.get(f"/v1/tenants/T001/tasks/{task_id}", headers=headers)
        status = r.json()["status"]
        if status in ("completed", "failed", "cancelled"):
            return r.json()
        time.sleep(0.3)
    raise TimeoutError(f"任务 {task_id} 未在 {timeout}s 内到达终态")


# ---------- 异步创建与执行 ----------

def test_create_returns_queued_immediately(client):
    """创建任务秒回（<1s 且 status=queued）"""
    headers = login(client)
    start = time.time()
    r = client.post("/v1/tenants/T001/tasks", headers=headers, json={
        "report_type": "EAST", "report_code": "E_ASYNC", "section": "个人住房贷款",
        "source_tables": ["loan_contract"], "target_table": "rpt_async",
    })
    elapsed = time.time() - start
    assert r.status_code == 200
    assert r.json()["status"] == "queued"
    assert elapsed < 1.0, f"创建耗时 {elapsed:.2f}s，未秒回"

    # worker 后台跑完 → completed（测试手动驱动一轮调度）
    drive_worker()
    final = wait_task(client, headers, r.json()["task_id"], timeout=5)
    assert final["status"] == "completed"
    assert final["progress"] == 100
    # 审计含 task.create
    data = client.get("/v1/tenants/T001/audit-logs?action=task.create&page_size=50",
                      headers=headers).json()
    assert any(l["resource"] == r.json()["task_id"] for l in data["logs"])


# ---------- 幂等 ----------

def test_idempotent_create(client):
    """相同 client_request_id 重复提交返回同一任务"""
    headers = login(client)
    payload = {"report_type": "EAST", "report_code": "E_IDEM", "section": "个人住房贷款",
               "source_tables": ["loan_contract"], "target_table": "rpt_idem",
               "client_request_id": "cr-idem-001"}
    r1 = client.post("/v1/tenants/T001/tasks", headers=headers, json=payload)
    r2 = client.post("/v1/tenants/T001/tasks", headers=headers, json=payload)
    assert r1.json()["task_id"] == r2.json()["task_id"]
    assert r2.json().get("idempotent") is True
    # 等待任务跑完，避免泄漏到后续用例
    drive_worker()
    wait_task(client, headers, r1.json()["task_id"], timeout=5)


# ---------- 取消 ----------

def test_cancel_queued_task(client):
    """取消 queued 任务 → 直接 cancelled，且不会再被 worker 拾取"""
    headers = login(client)

    # 直接落库 queued（独立事件循环，绕过 worker 抢跑的时间窗）
    async def _make():
        return await task_service.create_queued_task(
            "TASK_CANCEL_Q", "T001", {"report_type": "1104", "report_code": "C_Q"},
            created_by="admin")
    state = asyncio.run(_make())
    assert state["status"] == "queued"

    r = client.post("/v1/tenants/T001/tasks/TASK_CANCEL_Q/cancel", headers=headers)
    assert r.status_code == 200
    assert r.json()["status"] == "cancelled"

    # 审计含 task.cancel
    data = client.get("/v1/tenants/T001/audit-logs?action=task.cancel", headers=headers).json()
    assert any(l["resource"] == "TASK_CANCEL_Q" for l in data["logs"])


def test_cancel_executing_task(client):
    """executing 任务设置取消标记 → 阶段边界优雅终止为 cancelled"""
    async def _scenario():
        # 构造 executing 任务并直接设置取消标记
        await task_service.create_queued_task(
            "TASK_CANCEL_E", "T001",
            {"task_id": "TASK_CANCEL_E", "report_type": "EAST", "report_code": "C_E",
             "section": "个人住房贷款", "source_tables": ["loan_contract"],
             "target_table": "rpt_ce"},
            created_by="admin")
        state = await task_service.get_task_state("TASK_CANCEL_E")
        state["status"] = "executing"
        state["cancel_requested"] = True
        await task_service.save_task_state(state)
        # 直接驱动执行（worker 语义）：阶段边界应立即检测取消
        return await run_task(state)

    final = asyncio.run(_scenario())
    assert final["status"] == "cancelled"
    assert "取消" in (final["error"] or "")


def test_cancel_terminal_409(client):
    """终态任务不可取消 → 409"""
    headers = login(client)
    r = client.post("/v1/tenants/T001/tasks/TASK_CANCEL_Q/cancel", headers=headers)
    assert r.status_code == 409


# ---------- 断点恢复（kill 重启续跑） ----------

def test_resume_from_checkpoint(client):
    """模拟进程死亡：executing 任务带断点 → 恢复后从下一阶段续跑，已完成阶段不重复执行"""
    async def _scenario():
        from backend.core.orchestrator import TaskOrchestrator

        task_id = "TASK_RESUME_001"
        ctx = {"task_id": task_id, "report_type": "EAST", "report_code": "R001",
               "section": "个人住房贷款", "source_tables": ["loan_contract"],
               "target_table": "rpt_resume"}

        # 1) 真实执行第一阶段，拿到真实产出作为断点上下文
        orch = TaskOrchestrator("T001")
        parser_result = await orch._execute_agent("regulation_parser", ctx, {})
        assert parser_result.status == "success"

        # 2) 构造"进程死亡现场"：executing + 已完成 regulation_parser 的断点
        await task_service.create_queued_task(task_id, "T001", ctx, created_by="admin")
        state = await task_service.get_task_state(task_id)
        state.update({
            "status": "executing",
            "current_stage": "regulation_parser",
            "progress": 15,
            "stages": [parser_result.to_dict()],
            "outputs": {"regulation_parser": parser_result.output},
            "checkpoint": {"completed": ["regulation_parser"], "next": ["codegen"]},
        })
        await task_service.save_task_state(state)

        # 3) 模拟 worker 重启：恢复扫描 → 应回退为 queued 等待续跑
        worker = TaskWorker()
        await worker._recover_interrupted_tasks()
        recovered = await task_service.get_task_state(task_id)
        assert recovered["status"] == "queued"

        # 4) 断点续跑
        recovered["status"] = "executing"
        final = await run_task(recovered, resume=True)
        return final

    final = asyncio.run(_scenario())
    gate_info = [(s["agent_name"],
                  s["output"].get("gate_result", s["output"].get("fail_reasons"))
                  if isinstance(s.get("output"), dict) else None)
                 for s in final["stages"]]
    assert final["status"] == "completed", f"续跑失败: {final.get('error')} | 阶段: {gate_info}"
    # regulation_parser 只执行过一次（断点前的阶段不重复执行）
    parser_stages = [s for s in final["stages"] if s["agent_name"] == "regulation_parser"]
    assert len(parser_stages) == 1
    # 后续阶段都已执行
    agent_names = {s["agent_name"] for s in final["stages"]}
    assert {"codegen", "quality_gate", "test_verify", "digital_twin", "deploy"} <= agent_names


def test_recover_cancelled_while_down(client):
    """进程死亡前已请求取消的 executing 任务 → 恢复时直接终结 cancelled"""
    async def _scenario():
        await task_service.create_queued_task(
            "TASK_RESUME_CANCEL", "T001",
            {"report_type": "1104", "report_code": "RC"}, created_by="admin")
        state = await task_service.get_task_state("TASK_RESUME_CANCEL")
        state.update({
            "status": "executing",
            "cancel_requested": True,
            "checkpoint": {"completed": [], "next": ["regulation_parser"]},
        })
        await task_service.save_task_state(state)

        worker = TaskWorker()
        await worker._recover_interrupted_tasks()
        return await task_service.get_task_state("TASK_RESUME_CANCEL")

    state = asyncio.run(_scenario())
    assert state["status"] == "cancelled"


# ---------- 并发上限 ----------

def test_concurrency_limit(client):
    """全局并发上限生效：max=1 时同时只执行 1 个任务"""
    async def _scenario():
        # 临时把并发上限调为 1
        old = settings.task_worker_max_concurrency
        settings.task_worker_max_concurrency = 1
        try:
            for i in range(2):
                await task_service.create_queued_task(
                    f"TASK_CONC_{i}", "T001",
                    {"task_id": f"TASK_CONC_{i}", "report_type": "EAST",
                     "report_code": f"CC{i}", "section": "个人住房贷款",
                     "source_tables": ["loan_contract"], "target_table": f"rpt_cc{i}"}, created_by="admin")
            worker = TaskWorker()
            await worker._schedule_once()
            # 上限 1：只应有 1 个进入 executing，另 1 个仍 queued
            executing = await task_service.count_executing()
            queued = [t for t in await task_service.fetch_queued_tasks(10)
                      if t["task_id"].startswith("TASK_CONC_")]
            assert executing == 1, f"executing={executing}，并发上限未生效"
            assert len(queued) == 1
            # 清理：等执行中的任务跑完
            await asyncio.gather(*list(worker._running), return_exceptions=True)
        finally:
            settings.task_worker_max_concurrency = old

    asyncio.run(_scenario())
    headers = login(client)
    # 全局 worker 会拾取剩余的 TASK_CONC_1，手动驱动兜底
    drive_worker()
    for i in range(2):
        final = wait_task(client, headers, f"TASK_CONC_{i}", timeout=5)
        assert final["status"] == "completed"
