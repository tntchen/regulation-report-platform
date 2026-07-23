"""
历史方案库（范围 D）测试
覆盖：任务 completed 沉淀（含幂等）、同包推荐排序、跨包降权、API 契约、钩子容错

运行方式: python -m pytest tests/test_solution_library.py -v
"""

import asyncio
import os
import tempfile
from datetime import datetime, timedelta

# 在导入 app 之前切换到临时测试库 + 固定 JWT 密钥（与其他测试同模式）
_tmpdir = tempfile.mkdtemp(prefix="rrp_test_sol_")
os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{_tmpdir}/test_platform.db")
os.environ.setdefault("UPLOAD_DIR", f"{_tmpdir}/tenants")
os.environ.setdefault("DEMO_DB_PATH", f"{_tmpdir}/demo_biz.db")
os.environ.setdefault("TASK_WORK_DIR", f"{_tmpdir}/tasks")
os.environ.setdefault("LOG_DIR", f"{_tmpdir}/logs")
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-pytest")
os.environ.setdefault("DEBUG", "false")

import pytest
from fastapi.testclient import TestClient

from backend.config import settings
from backend.main import app
from backend.database import PlatformSessionLocal
from backend.models.solution_case import SolutionCase
from backend.services import solution_library, report_pack_service

settings.task_worker_enabled = False


@pytest.fixture(scope="module")
def client():
    """TestClient 上下文触发 lifespan（建表 + 种子用户）"""
    with TestClient(app) as c:
        yield c


def login(client, username="admin", password="Admin@1234"):
    r = client.post("/v1/auth/login", json={"username": username, "password": password})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _completed_state(task_id, pack_id="G01", report_type="1104", report_code="G01_A"):
    """构造一份 completed 任务终态（模拟编排器 outputs 结构）"""
    return {
        "task_id": task_id,
        "tenant_id": "T001",
        "status": "completed",
        "report_config": {
            "report_type": report_type,
            "report_code": report_code,
            "report_pack_id": pack_id,
            "target_table": "rpt_g01_housing_loan",
        },
        "outputs": {
            "regulation_parser": {
                "report_pack_id": pack_id,
                "mapping_suggestions": [
                    {"target_field": "loan_balance", "confidence": 0.92},
                    {"target_field": "org_no", "confidence": 0.70},
                ],
            },
            "quality_gate": {"gate_result": "pass"},
            "test_verify": {"critical_fail": False, "pass_count": 5},
            "digital_twin": {"reconciliation": {"余额勾稽": "pass"}},
        },
    }


def _seed_case(case_id, task_id, pack_id, report_type, minutes_ago=0):
    """直接插库造案例（指定创建时间，便于排序断言）"""

    async def _run():
        async with PlatformSessionLocal() as session:
            session.add(SolutionCase(
                id=case_id, tenant_id="T001", report_pack_id=pack_id,
                task_id=task_id, status="completed", created_by="tester",
                summary={"report_type": report_type, "gate_result": "pass"},
                created_at=datetime.now() - timedelta(minutes=minutes_ago),
            ))
            await session.commit()

    asyncio.run(_run())


# ---------- 沉淀 ----------

def test_record_case_on_completed(client):
    """completed 任务沉淀案例：摘要含映射/门禁/勾稽；重复沉淀幂等返回同一案例"""
    state = _completed_state("TASK_SOL_001")
    case_id = asyncio.run(solution_library.record_case_from_state(state))
    assert case_id and case_id.startswith("SC_")

    # 幂等：同一任务不重复落库
    again = asyncio.run(solution_library.record_case_from_state(state))
    assert again == case_id

    async def _load():
        async with PlatformSessionLocal() as session:
            return await session.get(SolutionCase, case_id)

    row = asyncio.run(_load())
    assert row is not None
    assert row.tenant_id == "T001"
    assert row.report_pack_id == "G01"
    assert row.task_id == "TASK_SOL_001"
    assert row.summary["gate_result"] == "pass"
    assert row.summary["mapping"]["suggestion_count"] == 2
    assert row.summary["mapping"]["high_confidence_count"] == 1
    assert row.summary["reconciliation"] == {"余额勾稽": "pass"}
    assert row.summary["report_type"] == "1104"


def test_record_case_skips_non_completed(client):
    """非 completed 状态不沉淀；缺 task_id 也不沉淀"""
    state = _completed_state("TASK_SOL_002")
    state["status"] = "failed"
    assert asyncio.run(solution_library.record_case_from_state(state)) is None
    assert asyncio.run(solution_library.record_case_from_state(
        {"status": "completed", "tenant_id": "T001"})) is None


# ---------- 推荐 ----------

def test_recommend_same_pack_ordering(client):
    """同包推荐：全部相似度 1.0，按创建时间倒序"""
    asyncio.run(report_pack_service.seed_builtin_packs())
    _seed_case("SC_T01", "TASK_SOL_R1", "G01", "1104", minutes_ago=30)
    _seed_case("SC_T02", "TASK_SOL_R2", "G01", "1104", minutes_ago=10)

    results = asyncio.run(solution_library.recommend("T001", "G01", limit=5))
    same_pack = [r for r in results if r["report_pack_id"] == "G01"]
    assert same_pack, "应至少推荐到同包案例"
    assert all(r["similarity"] == 1.0 for r in same_pack)
    # 同包内部按时间倒序：R2（较新）在 R1 之前
    ids = [r["task_id"] for r in same_pack]
    assert ids.index("TASK_SOL_R2") < ids.index("TASK_SOL_R1")


def test_recommend_cross_pack_downgrade(client):
    """跨包降权：同类型报表 0.6 排在同包 1.0 之后；不同类型不推荐"""
    _seed_case("SC_T03", "TASK_SOL_R3", "G11", "1104", minutes_ago=1)   # 同类型(EAST 之外)1104 → 0.6
    _seed_case("SC_T04", "TASK_SOL_R4", "EAST_JJ", "EAST", minutes_ago=1)  # 不同类型 → 不推荐

    results = asyncio.run(solution_library.recommend("T001", "G01", limit=10))
    by_task = {r["task_id"]: r for r in results}
    assert "TASK_SOL_R3" in by_task
    assert by_task["TASK_SOL_R3"]["similarity"] == 0.6
    assert "TASK_SOL_R4" not in by_task
    # 0.6 案例排在所有 1.0 案例之后
    first_low = next(i for i, r in enumerate(results) if r["similarity"] < 1.0)
    assert all(r["similarity"] == 1.0 for r in results[:first_low])


def test_recommend_tenant_isolation(client):
    """租户隔离：其他租户的案例不出现在推荐里"""

    async def _run():
        async with PlatformSessionLocal() as session:
            session.add(SolutionCase(
                id="SC_T99", tenant_id="T999", report_pack_id="G01",
                task_id="TASK_OTHER_TENANT", status="completed",
                summary={"report_type": "1104"},
                created_at=datetime.now(),
            ))
            await session.commit()

    asyncio.run(_run())
    results = asyncio.run(solution_library.recommend("T001", "G01", limit=20)
                          )
    assert all(r["task_id"] != "TASK_OTHER_TENANT" for r in results)


# ---------- API 契约 ----------

def test_recommend_api_contract(client):
    """GET /tasks/recommend：契约字段齐全 + 鉴权 + 缺参 400"""
    headers = login(client)
    _seed_case("SC_T05", "TASK_SOL_API", "G01", "1104", minutes_ago=5)

    r = client.get("/v1/tenants/T001/tasks/recommend?report_pack_id=G01",
                   headers=headers)
    assert r.status_code == 200
    tasks = r.json()["similar_tasks"]
    assert isinstance(tasks, list) and tasks
    item = next(t for t in tasks if t["task_id"] == "TASK_SOL_API")
    for key in ("task_id", "report_pack_id", "status", "created_at",
                "similarity", "summary"):
        assert key in item
    assert item["report_pack_id"] == "G01"
    assert item["similarity"] == 1.0

    # 未认证 → 401；缺 report_pack_id → 400
    assert client.get("/v1/tenants/T001/tasks/recommend?report_pack_id=G01"
                      ).status_code == 401
    assert client.get("/v1/tenants/T001/tasks/recommend",
                      headers=headers).status_code == 400


# ---------- 钩子容错 ----------

def test_orchestrator_hook_fault_tolerance(client, monkeypatch):
    """沉淀钩子容错：record_case_from_state 抛异常时不阻断，仅记日志"""
    from backend.core.orchestrator import TaskOrchestrator

    async def _boom(state):
        raise RuntimeError("数据库炸了")

    monkeypatch.setattr(solution_library, "record_case_from_state", _boom)

    async def _run():
        orch = TaskOrchestrator("T001")
        await orch._settle_solution_case(_completed_state("TASK_SOL_HOOK"))
        return "ok"

    # 不抛异常即通过
    assert asyncio.run(_run()) == "ok"
