"""
HITL 映射工作台 + 编排暂停/确认/恢复 集成测试（范围C）
覆盖（契约 docs/映射工作台与场景包设计方案.md §1.4/§2.4/§2.5）：
  1. 暂停→确认→恢复全链路（waiting_confirmation → confirm-all → 断点续跑至 completed）
  2. auto_mode=True 且全部高置信 → 不暂停（回归现有演示路径）
  3. 存在未处理 unmapped/rejected 时 confirm-all 拒绝（409）
  4. 确认后 mapping_assets 落库，再次确认同键资产 use_count+1
  5. 断点续跑从 codegen 开始（regulation_parser 不重复执行）

依赖说明：field_mappings/mapping_assets 模型、mapping_engine（范围B）与
report_pack_service（范围A）并行开发。测试优先使用真实模块；未就绪时按设计
方案 §1.2/§2.3 契约注入最小 stub（仅测试进程内，不写仓库文件），保证本测试
在任何整合阶段都可运行。推断引擎与场景包读取一律 monkeypatch，与 B/A 内部
实现解耦，只验证范围C的编排与 API 行为。

运行方式: python -m pytest tests/test_hitl.py -v
"""

import asyncio
import os
import sys
import tempfile
import types

# 在导入 app 之前切换到临时测试库 + 固定 JWT 密钥（与 test_task_async.py 同模式）
_tmpdir = tempfile.mkdtemp(prefix="rrp_test_hitl_")
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


def _ensure_mapping_deps():
    """范围B/A 依赖检测：真实模块缺失时按契约注入最小 stub（仅测试进程内）"""
    try:
        import backend.models.field_mapping  # noqa: F401
        import backend.models.mapping_asset  # noqa: F401
        import backend.services.mapping_engine  # noqa: F401
        import backend.services.report_pack_service  # noqa: F401
        return  # 真实依赖就绪，直接用
    except ImportError:
        pass

    from datetime import datetime
    from sqlalchemy import Column, String, Integer, Float, DateTime, JSON, UniqueConstraint
    from backend.database import Base

    # 部分就绪场景（如模型已存在但服务未就绪）：逐块守卫，避免表/模块重复定义
    if "field_mappings" not in Base.metadata.tables:
        # --- 契约 stub：models/field_mapping.py（设计方案 §1.2） ---
        class FieldMapping(Base):
            __tablename__ = "field_mappings"
            __table_args__ = (UniqueConstraint("task_id", "target_field"),)
            id = Column(String(40), primary_key=True)
            task_id = Column(String(32), nullable=False, index=True)
            report_pack_id = Column(String(32), nullable=False)
            target_field = Column(String(100), nullable=False)
            source_table = Column(String(100))
            source_field = Column(String(100))
            transform_rule = Column(String(500), default="DIRECT")
            confidence = Column(Float, default=0.0)
            evidence = Column(JSON, default={})
            status = Column(String(20), default="ai_inferred")
            confirmed_by = Column(String(50))
            confirmed_at = Column(DateTime)
            created_at = Column(DateTime, default=datetime.now)
            updated_at = Column(DateTime, default=datetime.now)

        fm_mod = types.ModuleType("backend.models.field_mapping")
        fm_mod.FieldMapping = FieldMapping
        sys.modules["backend.models.field_mapping"] = fm_mod

    if "mapping_assets" not in Base.metadata.tables:
        # --- 契约 stub：models/mapping_asset.py（设计方案 §1.3） ---
        class MappingAsset(Base):
            __tablename__ = "mapping_assets"
            __table_args__ = (UniqueConstraint(
                "report_pack_id", "target_field", "source_table", "source_field"),)
            id = Column(String(40), primary_key=True)
            report_pack_id = Column(String(32), nullable=False, index=True)
            target_field = Column(String(100), nullable=False)
            source_table = Column(String(100), default="")
            source_field = Column(String(100), default="")
            transform_rule = Column(String(500), default="DIRECT")
            use_count = Column(Integer, default=1)
            last_confirmed_by = Column(String(50))
            last_confirmed_at = Column(DateTime)

        ma_mod = types.ModuleType("backend.models.mapping_asset")
        ma_mod.MappingAsset = MappingAsset
        sys.modules["backend.models.mapping_asset"] = ma_mod

    # --- 契约 stub：services/mapping_engine.py（设计方案 §2.3 签名） ---
    try:
        import backend.services.mapping_engine  # noqa: F401
    except ImportError:
        me_mod = types.ModuleType("backend.services.mapping_engine")

        def infer_mappings(report_pack, schemas):  # 默认空推断；测试一律 monkeypatch
            return []

        me_mod.infer_mappings = infer_mappings
        sys.modules["backend.services.mapping_engine"] = me_mod

    # --- 契约 stub：services/report_pack_service.py ---
    try:
        import backend.services.report_pack_service  # noqa: F401
    except ImportError:
        rp_mod = types.ModuleType("backend.services.report_pack_service")

        async def get_report_pack(tenant_id, pack_id):  # 默认无包；测试一律 monkeypatch
            return None

        rp_mod.get_report_pack = get_report_pack
        sys.modules["backend.services.report_pack_service"] = rp_mod


_ensure_mapping_deps()

from backend.main import app  # noqa: E402
from backend.services import report_pack_service  # noqa: E402
from backend.services.mapping_engine import MappingEngine  # noqa: E402

# 关闭应用内 worker 自动轮询：测试全部手动驱动调度，消除时序竞态
settings.task_worker_enabled = False

# 测试用场景包（内容仅供编排读取，推断结果由 monkeypatch 决定）
FAKE_PACK = {
    "id": "G_TEST",
    "report_name": "测试场景包",
    "target_table": "rpt_hitl",
    "source_tables": ["loan_contract"],
    "target_schema": [
        {"field": "cust_name", "data_type": "varchar", "required": True},
        {"field": "loan_amt", "data_type": "decimal", "required": True},
    ],
}


def make_mappings(conf_status_pairs):
    """生成 monkeypatch 用的推断结果 [(confidence, status), ...]"""
    return [
        {
            "target_field": f"field_{i}",
            "source_table": "loan_contract" if st != "unmapped" else None,
            "source_field": f"col_{i}" if st != "unmapped" else None,
            "transform_rule": "DIRECT",
            "confidence": conf,
            "evidence": {"name": conf, "comment": None, "profile": conf,
                         "semantic": conf, "history": None},
            "status": st,
        }
        for i, (conf, st) in enumerate(conf_status_pairs)
    ]


@pytest.fixture
def mock_inference(monkeypatch):
    """monkeypatch 场景包读取与映射推断（对齐 A/B 真实接口）；返回设置推断结果的回调"""
    async def fake_get_pack(pack_id):
        return dict(FAKE_PACK, id=pack_id)

    monkeypatch.setattr(report_pack_service, "get_pack", fake_get_pack)

    def set_result(pairs):
        async def fake_infer(self, report_pack, schemas, task_id="", **kwargs):
            return make_mappings(pairs)
        monkeypatch.setattr(MappingEngine, "infer_mappings", fake_infer)
    return set_result


def drive_worker():
    """手动驱动一轮调度并等待执行完（替代后台轮询，保证测试确定性）"""
    from backend.services.task_worker import TaskWorker

    async def _drive():
        w = TaskWorker()
        await w._schedule_once()
        if w._running:
            await asyncio.gather(*list(w._running), return_exceptions=True)
    asyncio.run(_drive())


@pytest.fixture(scope="module")
def client():
    """TestClient 上下文触发 lifespan（建表 + 种子用户）"""
    with TestClient(app) as c:
        yield c


def login(client, username="admin", password="Admin@1234"):
    r = client.post("/v1/auth/login", json={"username": username, "password": password})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def create_task(client, headers, code, auto_mode=False):
    r = client.post("/v1/tenants/T001/tasks", headers=headers, json={
        "report_type": "1104", "report_code": code, "section": "测试",
        "source_tables": ["loan_contract"], "target_table": f"rpt_{code.lower()}",
        "report_pack_id": "G_TEST", "auto_mode": auto_mode,
    })
    assert r.status_code == 200, r.text
    return r.json()["task_id"]


def task_status(client, headers, task_id):
    return client.get(f"/v1/tenants/T001/tasks/{task_id}", headers=headers).json()


# ---------- 1. 暂停→确认→恢复全链路 + 5. 断点从 codegen 续跑 ----------

def test_pause_confirm_resume_full_chain(client, mock_inference):
    """中置信映射 → 挂起 waiting_confirmation → confirm-all → 从 codegen 断点续跑至 completed"""
    headers = login(client)
    mock_inference([(0.95, "ai_inferred"), (0.62, "ai_inferred")])  # 非全部高置信
    task_id = create_task(client, headers, "HITL_FULL", auto_mode=False)

    drive_worker()
    state = task_status(client, headers, task_id)
    assert state["status"] == "waiting_confirmation", f"未挂起: {state['status']}"

    # 断点：下一阶段 codegen + 暂停原因
    from backend.services import task_service
    db_state = asyncio.run(task_service.get_task_state(task_id))
    assert db_state["checkpoint"]["next"] == ["codegen"]
    assert db_state["checkpoint"]["pause_reason"] == "mapping_confirmation"

    # 映射清单 API：含 evidence
    r = client.get(f"/v1/tenants/T001/tasks/{task_id}/mappings", headers=headers)
    assert r.status_code == 200
    data = r.json()
    assert data["total"] == 2
    assert all("evidence" in m and "profile" in m for m in data["mappings"])

    # confirm-all → queued
    r = client.post(f"/v1/tenants/T001/tasks/{task_id}/mappings/confirm-all", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "queued"
    assert r.json()["auto_confirmed"] == 2

    # worker 断点续跑至 completed
    drive_worker()
    final = task_status(client, headers, task_id)
    assert final["status"] == "completed", f"续跑失败: {final.get('error')}"

    # 断点从 codegen 开始：regulation_parser 只执行一次
    agent_names = [s["agent_name"] for s in final["stages"]]
    assert agent_names.count("regulation_parser") == 1
    assert {"codegen", "quality_gate", "test_verify", "digital_twin", "deploy"} <= set(agent_names)

    # 映射全部 confirmed
    data = client.get(f"/v1/tenants/T001/tasks/{task_id}/mappings", headers=headers).json()
    assert data["confirmed"] == data["total"] == 2

    # 审计含 mapping.confirm_all
    logs = client.get("/v1/tenants/T001/audit-logs?action=mapping.confirm_all",
                      headers=headers).json()
    assert any(l["resource"] == task_id for l in logs["logs"])


# ---------- 2. auto_mode=True 且全高置信 → 不暂停 ----------

def test_auto_mode_skips_pause(client, mock_inference):
    """全部 ≥0.85 且 auto_mode → 直接跑完，不进入 waiting_confirmation"""
    headers = login(client)
    mock_inference([(0.95, "ai_inferred"), (0.90, "ai_inferred")])
    task_id = create_task(client, headers, "HITL_AUTO", auto_mode=True)

    drive_worker()
    final = task_status(client, headers, task_id)
    assert final["status"] == "completed", f"auto_mode 任务未直接完成: {final.get('error')}"
    # 映射仍落库（供资产沉淀追溯），但任务未暂停
    data = client.get(f"/v1/tenants/T001/tasks/{task_id}/mappings", headers=headers).json()
    assert data["total"] == 2


# ---------- 3. 未处理 unmapped 时 confirm-all 拒绝 ----------

def test_confirm_all_rejected_when_unmapped(client, mock_inference):
    """存在 unmapped 映射 → confirm-all 409；needs-etl 处理后可恢复"""
    headers = login(client)
    mock_inference([(0.90, "ai_inferred"), (0.20, "unmapped")])
    task_id = create_task(client, headers, "HITL_BLOCK", auto_mode=False)

    drive_worker()
    assert task_status(client, headers, task_id)["status"] == "waiting_confirmation"

    # confirm-all → 409，且提示阻断明细
    r = client.post(f"/v1/tenants/T001/tasks/{task_id}/mappings/confirm-all", headers=headers)
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["blocking"][0]["status"] == "unmapped"

    # 处理阻断项：needs-etl（终态，不阻断）
    mappings = client.get(f"/v1/tenants/T001/tasks/{task_id}/mappings",
                          headers=headers).json()["mappings"]
    unmapped_id = [m for m in mappings if m["status"] == "unmapped"][0]["id"]
    r = client.post(f"/v1/tenants/T001/tasks/{task_id}/mappings/{unmapped_id}/needs-etl",
                    headers=headers, json={})
    assert r.status_code == 200
    assert r.json()["status"] == "needs_etl"

    # 再次 confirm-all → 放行并跑完
    r = client.post(f"/v1/tenants/T001/tasks/{task_id}/mappings/confirm-all", headers=headers)
    assert r.status_code == 200, r.text
    drive_worker()
    final = task_status(client, headers, task_id)
    assert final["status"] == "completed", f"续跑失败: {final.get('error')}"

    # 审计含 mapping.needs_etl
    logs = client.get("/v1/tenants/T001/audit-logs?action=mapping.needs_etl",
                      headers=headers).json()
    assert any(l["resource"] == unmapped_id for l in logs["logs"])


# ---------- 4. 确认后资产库落库 + 复用计数 ----------

def test_mapping_assets_accumulate(client, mock_inference):
    """confirm/modify 后写入 mapping_assets；同键再次确认 use_count+1"""
    headers = login(client)
    pairs = [(0.92, "ai_inferred"), (0.70, "ai_inferred")]
    # 本用例确认的资产键（与其他用例的同名字段资产区分）
    keys = [(f"field_{i}", "loan_contract", f"col_{i}") for i in range(len(pairs))]

    def asset_counts():
        assets = client.get("/v1/tenants/T001/mapping-assets?report_pack_id=G_TEST",
                            headers=headers).json()["assets"]
        return {(a["target_field"], a["source_table"], a["source_field"]): a["use_count"]
                for a in assets}

    # 基线（其他用例可能已沉淀同键资产）
    baseline = asset_counts()

    # 两轮：同包新任务 confirm-all → 同键资产各 +1
    for rnd in (1, 2):
        mock_inference(pairs)
        task = create_task(client, headers, f"HITL_AST{rnd}", auto_mode=False)
        drive_worker()
        assert task_status(client, headers, task)["status"] == "waiting_confirmation"
        r = client.post(f"/v1/tenants/T001/tasks/{task}/mappings/confirm-all", headers=headers)
        assert r.status_code == 200, r.text
        drive_worker()
        assert task_status(client, headers, task)["status"] == "completed"

    final = asset_counts()
    for key in keys:
        assert final.get(key, 0) == baseline.get(key, 0) + 2, \
            f"{key} 复用计数未按轮次递增: {baseline.get(key, 0)} -> {final.get(key, 0)}"


# ---------- 单条操作：confirm / modify / reject ----------

def test_single_mapping_operations(client, mock_inference):
    """单条 confirm（带 transform_rule 修正）+ modify + reject 状态流转与审计"""
    headers = login(client)
    mock_inference([(0.88, "ai_inferred"), (0.55, "ai_inferred"), (0.40, "unmapped")])
    task_id = create_task(client, headers, "HITL_OPS", auto_mode=False)
    drive_worker()
    assert task_status(client, headers, task_id)["status"] == "waiting_confirmation"

    mappings = client.get(f"/v1/tenants/T001/tasks/{task_id}/mappings",
                          headers=headers).json()["mappings"]
    by_conf = sorted(mappings, key=lambda m: -(m["confidence"] or 0))
    high, mid, unmapped = by_conf[0], by_conf[1], by_conf[2]

    # confirm（附带 transform_rule 修正）
    r = client.post(f"/v1/tenants/T001/tasks/{task_id}/mappings/{high['id']}/confirm",
                    headers=headers, json={"transform_rule": "ROUND(amt, 4)"})
    assert r.status_code == 200 and r.json()["status"] == "confirmed"

    # modify（指定新源字段）
    r = client.post(f"/v1/tenants/T001/tasks/{task_id}/mappings/{mid['id']}/modify",
                    headers=headers, json={"source_table": "loan_contract",
                                           "source_field": "bal_amt",
                                           "transform_rule": "DIRECT"})
    assert r.status_code == 200 and r.json()["status"] == "modified"

    # reject → 阻断态
    r = client.post(f"/v1/tenants/T001/tasks/{task_id}/mappings/{unmapped['id']}/reject",
                    headers=headers)
    assert r.status_code == 200 and r.json()["status"] == "rejected"

    # rejected 仍阻断 confirm-all
    r = client.post(f"/v1/tenants/T001/tasks/{task_id}/mappings/confirm-all", headers=headers)
    assert r.status_code == 409

    # reject 后重新 modify 解除阻断 → confirm-all 放行
    r = client.post(f"/v1/tenants/T001/tasks/{task_id}/mappings/{unmapped['id']}/modify",
                    headers=headers, json={"source_table": "loan_contract",
                                           "source_field": "ext_col"})
    assert r.status_code == 200
    r = client.post(f"/v1/tenants/T001/tasks/{task_id}/mappings/confirm-all", headers=headers)
    assert r.status_code == 200, r.text
    drive_worker()
    assert task_status(client, headers, task_id)["status"] == "completed"

    # 审计：confirm / modify / reject / confirm_all 均有留痕
    for action in ("mapping.confirm", "mapping.modify", "mapping.reject"):
        logs = client.get(f"/v1/tenants/T001/audit-logs?action={action}",
                          headers=headers).json()
        assert logs["total"] >= 1, f"缺少审计动作 {action}"


# ---------- confirm-all 状态机约束 ----------

def test_confirm_all_requires_waiting_status(client, mock_inference):
    """非 waiting_confirmation 任务调用 confirm-all → 409"""
    headers = login(client)
    mock_inference([(0.95, "ai_inferred")])
    task_id = create_task(client, headers, "HITL_STATE", auto_mode=True)
    drive_worker()
    assert task_status(client, headers, task_id)["status"] == "completed"

    r = client.post(f"/v1/tenants/T001/tasks/{task_id}/mappings/confirm-all", headers=headers)
    assert r.status_code == 409


def test_mappings_require_auth(client):
    """映射 API 未认证 → 401"""
    r = client.get("/v1/tenants/T001/tasks/TASK_X/mappings")
    assert r.status_code == 401
    r = client.post("/v1/tenants/T001/tasks/TASK_X/mappings/confirm-all")
    assert r.status_code == 401


def test_mappings_task_not_found(client):
    """任务不存在 → 404"""
    headers = login(client)
    r = client.get("/v1/tenants/T001/tasks/TASK_NOPE/mappings", headers=headers)
    assert r.status_code == 404
