"""
制度版本 Diff + 新旧逻辑回归测试（范围C）
覆盖：
- compare_documents：Markdown 标题分段的增/删/改识别、相似度、unchanged 判定
- affected_keywords：从变更段落提取口径敏感关键词（金额/天数/分类/比例等）
- run_regression：1104 纯本金 vs EAST 含资本化利息 两条 SQL 变体的差异量化
- API：鉴权 401、regulations/diff 全流程、twin/regression 全流程

运行方式: python -m pytest tests/test_diff_regression.py -v
"""

import asyncio
import os
import tempfile

# 在导入 app 之前切换到临时测试库 + 固定 JWT 密钥（与其他测试同模式）
_tmpdir = tempfile.mkdtemp(prefix="rrp_test_diff_")
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
from backend.api.diff import diff_router
from backend.services import regulation_diff
from backend.agents.digital_twin import DigitalTwinAgent

settings.task_worker_enabled = False

# 路由注册由协调者统一做（main.py 归本范围）；测试内手动挂载幂等
app.include_router(diff_router, prefix="/v1")

TENANT = "T001"

# 1104 口径：纯本金余额；EAST 口径：本金 + 资本化利息（与数字孪生场景1一致的变体）
WHERE = ("is_deleted=0 AND is_test=0 AND org_no='1001' "
         "AND product_code IN ('P001','P001-G')")
SQL_OLD = (f"SELECT contract_no, ROUND(principal_balance, 4) AS balance "
           f"FROM loan_contract WHERE {WHERE}")
SQL_NEW = (f"SELECT contract_no, "
           f"ROUND(principal_balance + IFNULL(interest_capitalized, 0), 4) AS balance "
           f"FROM loan_contract WHERE {WHERE}")

OLD_DOC = """# 第一章 总则
本办法适用于个人住房贷款报送。
# 第二章 口径定义
贷款余额指报告期末纯本金余额，不含利息调整部分。
# 第三章 报送要求
按季报送，逾期天数按合同约定计算。
"""

NEW_DOC = """# 第一章 总则
本办法适用于个人住房贷款报送。
# 第二章 口径定义
贷款余额指报告期末账面余额，含资本化利息及利息调整部分。
五级分类比例按余额占比披露。
# 第四章 附则
本办法自 2027 年起施行。
"""


@pytest.fixture(scope="module")
def client():
    """TestClient 上下文触发 lifespan（建表 + 种子用户）"""
    with TestClient(app) as c:
        yield c


def login(client, username="admin", password="Admin@1234"):
    r = client.post("/v1/auth/login", json={"username": username, "password": password})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _create_doc(doc_id: str, content: str, filename: str):
    """直接落库一条文档记录 + 写正文文件（绕过上传接口，聚焦 diff 本身）"""
    from backend.database import PlatformSessionLocal
    from backend.models.document import RegulationDocument

    os.makedirs(settings.upload_dir, exist_ok=True)
    file_path = os.path.join(settings.upload_dir, f"{doc_id}_{filename}")
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(content)

    async def _insert():
        async with PlatformSessionLocal() as session:
            session.add(RegulationDocument(
                id=doc_id, tenant_id=TENANT, filename=filename,
                doc_type="1104", file_path=file_path, size=len(content),
                status="indexed", chunk_count=1, version=1, is_active=True,
            ))
            await session.commit()

    asyncio.run(_insert())
    return doc_id


# ---------- 文本 diff 单元 ----------

def test_compare_added_removed_changed():
    """增删改识别：第一章未变、第二章变更、第三章删除、第四章新增"""
    result = regulation_diff.compare_documents(OLD_DOC, NEW_DOC)
    assert result["added_sections"] == ["第四章 附则"]
    assert result["removed_sections"] == ["第三章 报送要求"]
    changed_titles = [c["title"] for c in result["changed_sections"]]
    assert changed_titles == ["第二章 口径定义"]
    assert "第一章 总则" in result["unchanged_sections"]
    # 变更节带相似度且介于 (0, 1)
    sim = result["changed_sections"][0]["similarity"]
    assert 0 < sim < 1
    # summary 可读
    assert "新增 1 节" in result["summary"]
    assert "删除 1 节" in result["summary"]
    assert "变更 1 节" in result["summary"]


def test_compare_identical_documents():
    """完全相同文档：无增删改，全部 unchanged"""
    result = regulation_diff.compare_documents(OLD_DOC, OLD_DOC)
    assert result["added_sections"] == []
    assert result["removed_sections"] == []
    assert result["changed_sections"] == []
    assert result["affected_keywords"] == []


def test_affected_keywords_extraction():
    """变更段落提取口径关键词：余额/利息/分类/比例/天数 等应命中"""
    result = regulation_diff.compare_documents(OLD_DOC, NEW_DOC)
    keywords = {k["keyword"] for k in result["affected_keywords"]}
    assert "余额" in keywords
    assert "利息" in keywords
    assert "比例" in keywords or "占比" in keywords
    assert "天数" in keywords  # 来自被删除的第三章
    # 每条关键词带节与上下文
    for item in result["affected_keywords"]:
        assert item["section"] and item["context"]


def test_compare_plain_text_without_headings():
    """无 Markdown 标题的纯文本：整体一节，内容不同即变更"""
    result = regulation_diff.compare_documents("旧口径：纯本金余额", "新口径：账面余额含利息")
    assert result["changed_sections"] and result["changed_sections"][0]["title"] == "（文档开头）"


# ---------- 回归场景单元 ----------

def test_run_regression_quantifies_diff():
    """1104 vs EAST 双 SQL：差异总额恰等于样本内资本化利息合计"""
    agent = DigitalTwinAgent()
    result = asyncio.run(agent.run_regression(
        {"id": "G01", "report_name": "测试场景包"}, SQL_OLD, SQL_NEW))
    # 样本内有效住房贷款 8 笔（剔除消费贷/测试/删除/他机构），
    # 资本化利息合计 1200+5600+2300+800+1500+4200 = 15600
    assert result["old_record_count"] == 8
    assert result["new_record_count"] == 8
    assert result["diff_amount"] == pytest.approx(15600.0, abs=0.01)
    assert result["new_total"] - result["old_total"] == pytest.approx(15600.0, abs=0.01)
    assert result["diff_rate"] > 0
    # 6 笔带资本化利息的记录存在差异
    assert result["level_distribution"]
    assert len(result["top_diffs"]) <= 5
    assert result["top_diffs"][0]["contract_no"] == "C003"  # 5600 为最大单笔差异
    assert "新旧逻辑回归" in result["conclusion"]


def test_run_regression_identical_sql_no_diff():
    """新旧 SQL 相同：差异为 0，结论为一致"""
    agent = DigitalTwinAgent()
    result = asyncio.run(agent.run_regression(
        {"id": "G01", "report_name": "测试场景包"}, SQL_OLD, SQL_OLD))
    assert result["diff_amount"] == 0
    assert result["diff_rate"] == 0
    assert result["top_diffs"] == []
    assert "一致" in result["conclusion"]


def test_run_regression_rejects_write_sql():
    """只读护栏：写操作 SQL 被拒绝，临时表不留残留"""
    agent = DigitalTwinAgent()
    with pytest.raises(PermissionError):
        asyncio.run(agent.run_regression(
            {"id": "G01"}, "DELETE FROM loan_contract", SQL_NEW))


# ---------- API ----------

def test_api_requires_auth(client):
    """两个端点未带 token 均 401"""
    r1 = client.post(f"/v1/tenants/{TENANT}/regulations/diff",
                     json={"doc_id_old": "a", "doc_id_new": "b"})
    r2 = client.post(f"/v1/tenants/{TENANT}/twin/regression",
                     json={"report_pack_id": "G01", "sql_old": SQL_OLD, "sql_new": SQL_NEW})
    assert r1.status_code == 401
    assert r2.status_code == 401


def test_regulations_diff_api(client):
    """制度 diff 全流程：造两份文档 → POST → 结构化结果"""
    headers = login(client)
    _create_doc("diff_old_001", OLD_DOC, "old.md")
    _create_doc("diff_new_001", NEW_DOC, "new.md")
    r = client.post(f"/v1/tenants/{TENANT}/regulations/diff",
                    json={"doc_id_old": "diff_old_001", "doc_id_new": "diff_new_001"},
                    headers=headers)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["added_sections"] == ["第四章 附则"]
    assert data["removed_sections"] == ["第三章 报送要求"]
    assert [c["title"] for c in data["changed_sections"]] == ["第二章 口径定义"]
    assert data["affected_keywords"]
    assert data["doc_old"]["filename"] == "old.md"


def test_regulations_diff_api_404(client):
    """文档不存在 404"""
    headers = login(client)
    r = client.post(f"/v1/tenants/{TENANT}/regulations/diff",
                    json={"doc_id_old": "nope_1", "doc_id_new": "nope_2"},
                    headers=headers)
    assert r.status_code == 404


def test_twin_regression_api(client):
    """回归 API 全流程：种子场景包 + 双 SQL → 差异量化"""
    from backend.services import report_pack_service
    asyncio.run(report_pack_service.seed_builtin_packs())

    headers = login(client)
    r = client.post(f"/v1/tenants/{TENANT}/twin/regression",
                    json={"report_pack_id": "G01", "sql_old": SQL_OLD, "sql_new": SQL_NEW},
                    headers=headers)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["diff_amount"] == pytest.approx(15600.0, abs=0.01)
    assert data["diff_rate"] > 0
    assert data["top_diffs"]
    assert "新旧逻辑回归" in data["conclusion"]


def test_twin_regression_api_rejects_bad_pack(client):
    """场景包不存在 404；写 SQL 被护栏 422"""
    headers = login(client)
    r = client.post(f"/v1/tenants/{TENANT}/twin/regression",
                    json={"report_pack_id": "NOPE", "sql_old": SQL_OLD, "sql_new": SQL_NEW},
                    headers=headers)
    assert r.status_code == 404
    r = client.post(f"/v1/tenants/{TENANT}/twin/regression",
                    json={"report_pack_id": "G01",
                          "sql_old": "DROP TABLE loan_contract", "sql_new": SQL_NEW},
                    headers=headers)
    assert r.status_code == 422
