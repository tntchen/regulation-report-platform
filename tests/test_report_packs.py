"""
场景包（Report Pack）测试
覆盖：包 CRUD API（鉴权/admin/审计）、种子幂等、Agent 1/2 读包驱动、
     G11 任务走通 Agent1-2、缺省 G01 兼容存量行为

运行方式: python -m pytest tests/test_report_packs.py -v
"""

import asyncio
import os
import tempfile

# 在导入 app 之前切换到临时测试库 + 固定 JWT 密钥（与其他测试同模式）
_tmpdir = tempfile.mkdtemp(prefix="rrp_test_packs_")
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
from backend.api.report_packs import report_packs_router
from backend.services import report_pack_service

settings.task_worker_enabled = False

# 路由注册由协调者统一做（main.py 归 C）；测试内手动挂载本范围路由
app.include_router(report_packs_router, prefix="/v1")


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


# ---------- 种子 ----------

def test_seed_builtin_packs_idempotent(client):
    """种子脚本幂等：首次灌入 3 个内置包，第二次运行返回空"""
    created = seed_packs()
    assert set(created) <= {"G01", "G11", "EAST_JJ"}
    # 幂等：第二次不再写入
    assert seed_packs() == []


def test_builtin_pack_contents(client):
    """内置包内容符合设计契约：G11 源表 loan_contract 且 trap_refs 含逾期90天"""
    g11 = asyncio.run(report_pack_service.get_pack("G11"))
    assert g11 is not None
    assert g11["report_type"] == "1104"
    assert g11["source_tables"] == ["loan_contract"]
    assert "逾期90天" in g11["trap_refs"]
    assert any(f["field"] == "five_classify" for f in g11["target_schema"])
    assert g11["regulation_keywords"]

    g01 = asyncio.run(report_pack_service.get_pack("G01"))
    assert g01["target_table"] == "rpt_g01_housing_loan"
    east = asyncio.run(report_pack_service.get_pack("EAST_JJ"))
    assert east["report_type"] == "EAST"


# ---------- API CRUD ----------

def test_list_and_get_packs(client):
    """GET 列表/详情：租户成员可读"""
    headers = login(client)
    r = client.get("/v1/tenants/T001/report-packs", headers=headers)
    assert r.status_code == 200
    ids = {p["id"] for p in r.json()["report_packs"]}
    assert {"G01", "G11", "EAST_JJ"} <= ids

    r = client.get("/v1/tenants/T001/report-packs/G11", headers=headers)
    assert r.status_code == 200
    assert r.json()["target_table"] == "rpt_g11_five_classify"
    assert r.json()["reconciliation_rules"]

    # 不存在 → 404；未认证 → 401
    assert client.get("/v1/tenants/T001/report-packs/NOPE",
                      headers=headers).status_code == 404
    assert client.get("/v1/tenants/T001/report-packs").status_code == 401


def test_create_pack_admin_only(client):
    """POST 仅 admin：创建成功 + 重复 409 + 审计落库；非 admin 403"""
    headers = login(client)
    payload = {
        "id": "T_PACK1", "report_name": "测试场景包", "report_type": "1104",
        "target_table": "rpt_t_pack1",
        "target_schema": [{"field": "f1", "data_type": "string",
                           "required": True, "caliber_text": "测试字段"}],
        "source_tables": ["loan_contract"],
        "regulation_keywords": "测试 关键词",
    }
    r = client.post("/v1/tenants/T001/report-packs", headers=headers, json=payload)
    assert r.status_code == 200
    assert r.json()["id"] == "T_PACK1"
    assert r.json()["target_schema"][0]["caliber_text"] == "测试字段"

    # 重复创建 → 409
    assert client.post("/v1/tenants/T001/report-packs",
                       headers=headers, json=payload).status_code == 409

    # 缺必填字段 → 422
    assert client.post("/v1/tenants/T001/report-packs", headers=headers,
                       json={"id": "T_PACK2"}).status_code == 422

    # 非 admin → 403
    z_headers = login(client, "zhangsan", "Zhangsan@1234")
    assert client.post("/v1/tenants/T001/report-packs", headers=z_headers,
                       json=payload).status_code == 403

    # 审计含 report_pack.create
    logs = client.get("/v1/tenants/T001/audit-logs?action=report_pack.create",
                      headers=headers).json()
    assert any(l["resource"] == "T_PACK1" for l in logs["logs"])


def test_update_pack(client):
    """PUT 部分更新：详情立即读到新值（缓存失效）+ 审计落库"""
    headers = login(client)
    r = client.put("/v1/tenants/T001/report-packs/T_PACK1", headers=headers,
                   json={"report_name": "测试场景包-改", "status": "draft"})
    assert r.status_code == 200
    assert r.json()["report_name"] == "测试场景包-改"
    assert r.json()["status"] == "draft"

    r = client.get("/v1/tenants/T001/report-packs/T_PACK1", headers=headers)
    assert r.json()["report_name"] == "测试场景包-改"

    # ID 不可修改 → 422；不存在 → 404；非 admin → 403
    assert client.put("/v1/tenants/T001/report-packs/T_PACK1", headers=headers,
                      json={"id": "OTHER"}).status_code == 422
    assert client.put("/v1/tenants/T001/report-packs/NOPE", headers=headers,
                      json={"report_name": "x"}).status_code == 404
    z_headers = login(client, "zhangsan", "Zhangsan@1234")
    assert client.put("/v1/tenants/T001/report-packs/T_PACK1", headers=z_headers,
                      json={"report_name": "越权"}).status_code == 403

    logs = client.get("/v1/tenants/T001/audit-logs?action=report_pack.update",
                      headers=headers).json()
    assert any(l["resource"] == "T_PACK1" for l in logs["logs"])


# ---------- Agent 读包驱动 ----------

def _make_agents():
    """按编排器同款方式接线 Agent 1/2（Mock AI + 真实 MCP 服务）"""
    from backend.core.tenant_context import PRESET_TENANTS
    from backend.core.ai_adapter import AIAdapterFactory
    from backend.mcp.database_mcp import DatabaseMCPService
    from backend.mcp.regulation_rag import RegulationRAGService
    from backend.agents.regulation_parser import RegulationParserAgent
    from backend.agents.codegen import CodeGenAgent

    tenant_config = PRESET_TENANTS["T001"]
    mcp_services = {
        "database_mcp": DatabaseMCPService(tenant_config.get("data_sources", [{}])[0]),
        "regulation_rag": RegulationRAGService("T001"),
    }
    ai_backend = AIAdapterFactory.get_adapter("T001")

    parser = RegulationParserAgent()
    parser.set_mcp_tools(mcp_services)
    parser.set_ai_backend(ai_backend)
    codegen = CodeGenAgent()
    codegen.set_mcp_tools(mcp_services)
    codegen.set_ai_backend(ai_backend)
    return parser, codegen


def test_g11_task_agents_1_2(client):
    """G11 任务走通 Agent1-2：检索关键词/目标结构/勾稽规则均来自场景包"""
    seed_packs()

    async def _run():
        parser, codegen = _make_agents()
        ctx = {"task_id": "TASK_G11_T", "tenant_id": "T001",
               "report_pack_id": "G11", "section": "资产质量五级分类"}
        r1 = await parser.execute(ctx)
        assert r1.status == "success", r1.error
        r2 = await codegen.execute(ctx, r1.output)
        assert r2.status == "success", r2.error
        return ctx, r1, r2

    ctx, r1, r2 = asyncio.run(_run())

    # Agent 1：包驱动检索与映射建议
    assert r1.output["report_pack_id"] == "G11"
    assert "G11" in r1.output["retrieval_query"] or "五级分类" in r1.output["retrieval_query"]
    fields = {m["target_field"] for m in r1.output["mapping_suggestions"]}
    assert "five_classify" in fields
    # 包陷阱关键词注入（逾期90天）
    assert any("逾期90天" in t.get("description", "") for t in r1.output["traps_identified"])
    # 包定义回填任务上下文（供下游 Agent 共享）
    assert ctx["target_table"] == "rpt_g11_five_classify"
    assert ctx["source_tables"] == ["loan_contract"]

    # Agent 2：目标表来自包定义（Mock AI 从 Prompt 解析目标表生成 SQL）
    assert r2.output["report_pack_id"] == "G11"
    assert "rpt_g11_five_classify" in r2.output["generated_code"]
    assert "loan_contract" in r2.output["source_schemas"]


def test_default_g01_compatibility(client):
    """存量兼容：不传 report_pack_id 时缺省 G01，任务上下文字段被包回填"""
    seed_packs()

    async def _run():
        parser, codegen = _make_agents()
        # 存量调用形态：无 report_pack_id、无 source_tables/target_table
        ctx = {"task_id": "TASK_LEGACY", "tenant_id": "T001",
               "report_type": "EAST", "report_code": "E_OLD", "section": "个人住房贷款"}
        r1 = await parser.execute(ctx)
        assert r1.status == "success", r1.error
        return ctx, r1

    ctx, r1 = asyncio.run(_run())
    # 缺省 G01：包回填源表与目标表，但任务显式指定的 report_type 不被覆盖
    assert r1.output["report_pack_id"] == "G01"
    assert ctx["source_tables"] == ["loan_contract"]
    assert ctx["target_table"] == "rpt_g01_housing_loan"
    assert ctx["report_type"] == "EAST"
