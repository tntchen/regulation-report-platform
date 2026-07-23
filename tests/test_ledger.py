"""
报送台账 + 截止期（Submission Ledger）测试
覆盖：生成幂等、状态流转（pending→in_progress→submitted）、逾期懒计算、
     API 契约 + 鉴权（未认证 401、越租户隔离）、审计落库

运行方式: python -m pytest tests/test_ledger.py -v
"""

import asyncio
import os
import tempfile
from datetime import datetime, timedelta

# 在导入 app 之前切换到临时测试库 + 固定 JWT 密钥（与其他测试同模式）
_tmpdir = tempfile.mkdtemp(prefix="rrp_test_ledger_")
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
from backend.api.ledger import ledger_router
from backend.services import ledger_service, report_pack_service
from backend.database import PlatformSessionLocal
from backend.models.submission_ledger import SubmissionLedger
from backend.models.task import Task

settings.task_worker_enabled = False

# 路由注册由协调者统一做（main.py 归 C）；测试内手动挂载本范围路由
app.include_router(ledger_router, prefix="/v1")

# 用未来的报送期间，避免逾期懒计算把 pending/in_progress 标成 overdue
_now = datetime.now()
TEST_PERIOD = f"{(_now.replace(day=1) + timedelta(days=32)).strftime('%Y-%m')}"   # 下月
TEST_PERIOD2 = f"{(_now.replace(day=1) + timedelta(days=63)).strftime('%Y-%m')}"  # 下下月


@pytest.fixture(scope="module")
def client():
    """TestClient 上下文触发 lifespan（建表 + 种子用户）"""
    with TestClient(app) as c:
        yield c


def login(client, username="admin", password="Admin@1234"):
    r = client.post("/v1/auth/login", json={"username": username, "password": password})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def seed_packs():
    """灌入内置场景包（幂等）"""
    return asyncio.run(report_pack_service.seed_builtin_packs())


def make_task(task_id, tenant_id="T001"):
    """直接落一条任务记录（bind-task 的校验对象，不走任务 API 保持测试聚焦）"""
    async def _insert():
        async with PlatformSessionLocal() as session:
            session.add(Task(id=task_id, tenant_id=tenant_id,
                             task_type="report_gen", name="台账绑定测试任务",
                             status="created", created_by="admin"))
            await session.commit()
    asyncio.run(_insert())


# ---------- 截止期规则 ----------

def test_compute_deadline():
    """月报截止期 = 次月 5 日 23:59:59；12 月跨年到次年 1 月"""
    d = ledger_service.compute_deadline("2025-06")
    assert d == datetime(2025, 7, 5, 23, 59, 59)
    d = ledger_service.compute_deadline("2025-12")
    assert d == datetime(2026, 1, 5, 23, 59, 59)
    # 可配：截止日可改为次月 10 日
    d = ledger_service.compute_deadline("2025-06", deadline_day=10)
    assert d == datetime(2025, 7, 10, 23, 59, 59)
    assert ledger_service.validate_period("2025-06")
    assert not ledger_service.validate_period("2025-13")
    assert not ledger_service.validate_period("202506")


# ---------- 生成幂等 ----------

def test_generate_idempotent(client):
    """generate_ledger 幂等：首次为每个 active 包建一条，重复调用全部 skipped"""
    seed_packs()
    r1 = asyncio.run(ledger_service.generate_ledger("T001", TEST_PERIOD))
    assert r1["created"] >= 3  # G01/G11/EAST_JJ
    assert r1["skipped"] == 0

    r2 = asyncio.run(ledger_service.generate_ledger("T001", TEST_PERIOD))
    assert r2["created"] == 0
    assert r2["skipped"] == r1["created"]

    entries = asyncio.run(ledger_service.list_ledger("T001", TEST_PERIOD))
    assert len(entries) == r1["created"]
    for e in entries:
        assert e["period"] == TEST_PERIOD
        assert e["status"] == "pending"
        assert e["task_id"] is None and e["submitted_at"] is None
        assert e["deadline"] == ledger_service.compute_deadline(TEST_PERIOD).isoformat()

    # 非法 period
    with pytest.raises(ValueError):
        asyncio.run(ledger_service.generate_ledger("T001", "2025-13"))


# ---------- 状态流转 ----------

def test_status_flow(client):
    """pending → bind_task → in_progress → submit → submitted（重复报送幂等）"""
    make_task("TASK_LEDGER_1")
    entries = asyncio.run(ledger_service.list_ledger("T001", TEST_PERIOD))
    entry_id = next(e["id"] for e in entries if e["report_pack_id"] == "G01")

    # bind_task：pending → in_progress
    e = asyncio.run(ledger_service.bind_task(entry_id, "T001", "TASK_LEDGER_1"))
    assert e["status"] == "in_progress"
    assert e["task_id"] == "TASK_LEDGER_1"

    # 重复绑定同任务：保持 in_progress
    e = asyncio.run(ledger_service.bind_task(entry_id, "T001", "TASK_LEDGER_1"))
    assert e["status"] == "in_progress"

    # submit → submitted
    e = asyncio.run(ledger_service.submit_entry(entry_id, "T001"))
    assert e["status"] == "submitted"
    assert e["submitted_at"] is not None

    # 重复报送幂等
    e2 = asyncio.run(ledger_service.submit_entry(entry_id, "T001"))
    assert e2["status"] == "submitted"

    # 已报送不可再绑定任务
    with pytest.raises(ValueError):
        asyncio.run(ledger_service.bind_task(entry_id, "T001", "TASK_LEDGER_1"))

    # 绑定不存在的任务 / 越租户任务
    other = next(e["id"] for e in entries if e["report_pack_id"] == "G11")
    with pytest.raises(LookupError):
        asyncio.run(ledger_service.bind_task(other, "T001", "TASK_NOPE"))
    make_task("TASK_LEDGER_T2", tenant_id="T002")
    with pytest.raises(LookupError):
        asyncio.run(ledger_service.bind_task(other, "T001", "TASK_LEDGER_T2"))

    # 越租户访问条目 → None
    assert asyncio.run(ledger_service.submit_entry(entry_id, "T002")) is None


# ---------- 逾期懒计算 ----------

def test_overdue_lazy_compute(client):
    """超 deadline 且未 submitted → overdue（懒计算不落库）；已报送不受影响"""
    async def _make():
        async with PlatformSessionLocal() as session:
            row = SubmissionLedger(
                id="LEDGER_OVERDUE1", tenant_id="T001", report_pack_id="G01",
                report_name="逾期测试报表", period="2025-01",
                deadline=datetime.now() - timedelta(days=10), status="pending",
            )
            row2 = SubmissionLedger(
                id="LEDGER_OVERDUE2", tenant_id="T001", report_pack_id="G11",
                report_name="逾期已报送报表", period="2025-01",
                deadline=datetime.now() - timedelta(days=10),
                status="submitted", submitted_at=datetime.now() - timedelta(days=15),
            )
            session.add_all([row, row2])
            await session.commit()

    asyncio.run(_make())

    entries = {e["id"]: e for e in asyncio.run(ledger_service.list_ledger("T001", "2025-01"))}
    assert entries["LEDGER_OVERDUE1"]["status"] == "overdue"
    assert entries["LEDGER_OVERDUE1"]["days_left"] < 0
    assert entries["LEDGER_OVERDUE2"]["status"] == "submitted"

    # 懒计算不改库：库里仍是 pending
    async def _check():
        async with PlatformSessionLocal() as session:
            row = await session.get(SubmissionLedger, "LEDGER_OVERDUE1")
            return row.status
    assert asyncio.run(_check()) == "pending"

    # 逾期条目仍可报送
    e = asyncio.run(ledger_service.submit_entry("LEDGER_OVERDUE1", "T001"))
    assert e["status"] == "submitted"


# ---------- API 契约 + 鉴权 ----------

def test_api_contract_and_auth(client):
    """API：未认证 401；generate/submit/bind-task 契约 + 租户隔离 + 审计落库"""
    headers = login(client)
    base = "/v1/tenants/T001/ledger"
    period = TEST_PERIOD2  # 与上一用例不同的未来期间，避免幂等跳过影响断言

    # 未认证 → 401
    assert client.get(base).status_code == 401
    assert client.post(f"{base}/generate", json={"period": period}).status_code == 401

    # generate：缺 period / 非法 period → 422
    assert client.post(f"{base}/generate", headers=headers, json={}).status_code == 422
    assert client.post(f"{base}/generate", headers=headers,
                       json={"period": "2025/08"}).status_code == 422

    # generate 正常 + 幂等
    r = client.post(f"{base}/generate", headers=headers, json={"period": period})
    assert r.status_code == 200
    body = r.json()
    assert body["created"] >= 3 and body["skipped"] == 0
    assert len(body["entries"]) == body["created"]
    r = client.post(f"{base}/generate", headers=headers, json={"period": period})
    assert r.json()["created"] == 0 and r.json()["skipped"] >= 3

    # GET 列表契约
    r = client.get(f"{base}?period={period}", headers=headers)
    assert r.status_code == 200
    entries = r.json()["entries"]
    assert entries, "台账列表为空"
    for e in entries:
        assert set(e) == {"id", "report_pack_id", "report_name", "period", "deadline",
                          "status", "task_id", "submitted_at", "days_left"}
    # 非法 period 过滤 → 422
    assert client.get(f"{base}?period=bad", headers=headers).status_code == 422

    # bind-task（走 API 建任务记录）
    make_task("TASK_LEDGER_API")
    entry_id = entries[0]["id"]
    r = client.post(f"{base}/{entry_id}/bind-task", headers=headers,
                    json={"task_id": "TASK_LEDGER_API"})
    assert r.status_code == 200
    assert r.json()["status"] == "in_progress"
    assert r.json()["task_id"] == "TASK_LEDGER_API"

    # bind-task：缺 task_id 422 / 任务不存在 404 / 条目不存在 404
    assert client.post(f"{base}/{entry_id}/bind-task", headers=headers,
                       json={}).status_code == 422
    assert client.post(f"{base}/{entry_id}/bind-task", headers=headers,
                       json={"task_id": "NOPE"}).status_code == 404
    assert client.post(f"{base}/LEDGER_NOPE/bind-task", headers=headers,
                       json={"task_id": "TASK_LEDGER_API"}).status_code == 404

    # submit
    r = client.post(f"{base}/{entry_id}/submit", headers=headers)
    assert r.status_code == 200
    assert r.json()["status"] == "submitted"
    assert r.json()["submitted_at"] is not None
    assert client.post(f"{base}/LEDGER_NOPE/submit", headers=headers).status_code == 404
    # 已报送再 bind → 409
    assert client.post(f"{base}/{entry_id}/bind-task", headers=headers,
                       json={"task_id": "TASK_LEDGER_API"}).status_code == 409

    # 越租户：T002 看不到 T001 的条目
    assert client.post("/v1/tenants/T002/ledger/LEDGER_OVERDUE1/submit",
                       headers=headers).status_code in (403, 404)

    # 审计落库
    logs = client.get("/v1/tenants/T001/audit-logs?action=ledger.generate",
                      headers=headers).json()
    assert any(l["resource"] == period for l in logs["logs"])
    logs = client.get("/v1/tenants/T001/audit-logs?action=ledger.submit",
                      headers=headers).json()
    assert any(l["resource"] == entry_id for l in logs["logs"])
