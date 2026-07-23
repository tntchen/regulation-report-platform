"""
勾稽规则配置化（Agent 4 规则驱动对账）测试
覆盖：规则驱动 pass/fail/tolerance 边界、故意改坏数据触发 fail、
     无规则/包缺失时回退硬编码兼容、G11 内置包规则在种子数据上真实执行

运行方式: python -m pytest tests/test_reconciliation.py -v
"""

import asyncio
import os
import tempfile

# 在导入 app 之前切换到临时测试库 + 固定 JWT 密钥（与其他测试同模式）
_tmpdir = tempfile.mkdtemp(prefix="rrp_test_recon_")
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
from backend.services import report_pack_service
from backend.agents.test_verify import TestVerifyAgent

settings.task_worker_enabled = False

# 标准演示口径：源表有效行过滤条件（与 Agent 4 内部口径一致）
FILTER = ("is_deleted=0 AND is_test=0 AND org_no='1001' "
          "AND product_code IN ('P001','P001-G')")

# G11 目标表的“正确”生成 SQL：loan_balance = 本金 + 资本化利息，逐行映射
G11_TARGET = "rpt_g11_five_classify"
G11_GOOD_SQL = f"""
INSERT INTO {G11_TARGET} (contract_no, cust_id, loan_balance, five_classify, overdue_days, biz_date, org_no)
SELECT contract_no, cust_id,
       principal_balance + IFNULL(interest_capitalized, 0) AS loan_balance,
       five_classify, overdue_days, biz_date, org_no
FROM loan_contract
WHERE {FILTER}
"""


@pytest.fixture(scope="module")
def client():
    """TestClient 上下文触发 lifespan（建平台表 + 种子用户）"""
    with TestClient(app) as c:
        yield c


def run_agent(task_context, sql):
    """直接驱动 Agent 4：装载生成 SQL 并执行全部校验"""
    agent = TestVerifyAgent()
    return asyncio.run(agent.execute(task_context, codegen_output={"generated_code": sql}))


def get_check(output, check_id):
    return next(c for c in output["checks"] if c["check_id"] == check_id)


# ---------- 规则驱动 pass ----------

def test_rule_driven_reconcile_pass(client):
    """G11 包规则在正确 SQL 下全部通过：实测/期望/差异/通过标记齐备"""
    asyncio.run(report_pack_service.seed_builtin_packs())
    ctx = {"task_id": "TASK_RECON_PASS", "tenant_id": "T001",
           "report_pack_id": "G11", "source_tables": ["loan_contract"],
           "target_table": G11_TARGET}
    r = run_agent(ctx, G11_GOOD_SQL)
    assert r.status == "success", r.error

    check = get_check(r.output, "reconcile")
    assert check["status"] == "pass", check
    rules = check["metrics"]["rule_results"]
    assert check["metrics"]["rule_count"] == len(rules) >= 2
    names = {x["name"] for x in rules}
    assert "贷款余额勾稽" in names and "五级分类合计勾稽" in names
    for rule in rules:
        assert rule["passed"] is True
        assert rule["actual"] is not None and rule["expected"] is not None
        assert rule["abs_diff"] is not None and rule["abs_diff"] <= rule["tolerance"] + 1e-6


def test_g11_rules_real_execution_on_seed(client):
    """G11 包规则在种子数据上真实执行：分组勾稽数值与种子手工重算一致"""
    asyncio.run(report_pack_service.seed_builtin_packs())
    ctx = {"task_id": "TASK_RECON_G11", "tenant_id": "T001",
           "report_pack_id": "G11", "source_tables": ["loan_contract"],
           "target_table": G11_TARGET}
    r = run_agent(ctx, G11_GOOD_SQL)
    check = get_check(r.output, "reconcile")
    assert check["status"] == "pass"

    group_rule = next(x for x in check["metrics"]["rule_results"]
                      if x["name"] == "五级分类合计勾稽")
    groups = group_rule["actual"]
    # 种子有效行按五级分类手工重算（本金+资本化利息）：
    #   '1': C001 801200 + C002 300000 + C005 900000 + C008 501500 + C012 1104200 = 3606900
    #   '2': C003 1505600 + C007 450800 = 1956400
    #   '3': C004 602300
    assert set(groups) == {"1", "2", "3"}
    assert groups["1"]["actual"] == pytest.approx(3606900.0, abs=0.01)
    assert groups["1"]["expected"] == pytest.approx(3606900.0, abs=0.01)
    assert groups["2"]["actual"] == pytest.approx(1956400.0, abs=0.01)
    assert groups["3"]["actual"] == pytest.approx(602300.0, abs=0.01)
    assert all(g["passed"] for g in groups.values())


# ---------- 规则驱动 fail / 容差边界 ----------

def test_rule_driven_fail_on_corrupted_data(client):
    """故意改坏数据（漏掉一行）→ 余额与分组勾稽均 fail，触发关键失败门禁"""
    asyncio.run(report_pack_service.seed_builtin_packs())
    bad_sql = G11_GOOD_SQL.replace("WHERE", "WHERE contract_no <> 'C001' AND")
    ctx = {"task_id": "TASK_RECON_BAD", "tenant_id": "T001",
           "report_pack_id": "G11", "source_tables": ["loan_contract"],
           "target_table": G11_TARGET}
    r = run_agent(ctx, bad_sql)

    check = get_check(r.output, "reconcile")
    assert check["status"] == "fail"
    assert check["critical"] is True
    assert r.output["critical_fail"] is True
    assert r.output["overall_result"] == "fail"
    failed_rules = [x for x in check["metrics"]["rule_results"] if not x["passed"]]
    assert failed_rules, "漏行后应至少一条规则不通过"
    # 漏掉 C001（801200），余额勾稽实测应比期望小 801200
    bal = next(x for x in check["metrics"]["rule_results"] if x["name"] == "贷款余额勾稽")
    assert bal["passed"] is False
    assert bal["expected"] - bal["actual"] == pytest.approx(801200.0, abs=0.01)


def test_tolerance_boundary(client):
    """容差边界：差异恰等于 tolerance → 通过；略超 → 不通过"""
    asyncio.run(report_pack_service.seed_builtin_packs())
    # 自定义包：余额整体上浮 0.005，两条规则容差分别取边界两侧
    pack = {
        "id": "T_RECON_TOL", "report_name": "容差边界测试包", "report_type": "1104",
        "target_table": G11_TARGET,
        "target_schema": [], "source_tables": ["loan_contract"],
        "reconciliation_rules": [
            {"name": "边界内", "expression": "SUM(loan_balance)", "tolerance": 0.005},
            {"name": "边界外", "expression": "SUM(loan_balance)", "tolerance": 0.004},
        ],
        "regulation_keywords": "测试",
    }
    assert asyncio.run(report_pack_service.create_pack(pack)) is not None

    drift_sql = G11_GOOD_SQL.replace(
        "principal_balance + IFNULL(interest_capitalized, 0)",
        "principal_balance + IFNULL(interest_capitalized, 0) + 0.005")
    ctx = {"task_id": "TASK_RECON_TOL", "tenant_id": "T001",
           "report_pack_id": "T_RECON_TOL", "source_tables": ["loan_contract"],
           "target_table": G11_TARGET}
    r = run_agent(ctx, drift_sql)
    rules = {x["name"]: x for x in get_check(r.output, "reconcile")["metrics"]["rule_results"]}
    # 8 行 × 0.005 = 0.04 总漂移
    assert rules["边界内"]["abs_diff"] == pytest.approx(0.04, abs=1e-4)
    assert rules["边界内"]["passed"] is False   # 0.04 > 0.005
    assert rules["边界外"]["passed"] is False

    # 再把漂移降到恰等于容差：单行 +0.005，总差 0.005 = tolerance → 通过
    edge_sql = G11_GOOD_SQL.replace(
        "principal_balance + IFNULL(interest_capitalized, 0)",
        "principal_balance + IFNULL(interest_capitalized, 0)"
        " + CASE WHEN contract_no='C001' THEN 0.005 ELSE 0 END")
    r2 = run_agent(ctx, edge_sql)
    rules2 = {x["name"]: x for x in get_check(r2.output, "reconcile")["metrics"]["rule_results"]}
    assert rules2["边界内"]["abs_diff"] == pytest.approx(0.005, abs=1e-6)
    assert rules2["边界内"]["passed"] is True    # 差异恰等于容差，判定通过
    assert rules2["边界外"]["passed"] is False   # 0.005 > 0.004，判定不通过


# ---------- 回退兼容 ----------

def test_fallback_when_pack_missing(client):
    """report_pack_id 不存在 → 回退硬编码对账口径（target_total/source_total 指标）"""
    ctx = {"task_id": "TASK_RECON_NOPE", "tenant_id": "T001",
           "report_pack_id": "NO_SUCH_PACK", "source_tables": ["loan_contract"],
           "target_table": G11_TARGET}
    r = run_agent(ctx, G11_GOOD_SQL)
    check = get_check(r.output, "reconcile")
    assert check["status"] == "pass"
    # 硬编码口径的指标结构（非 rule_results）
    assert "target_total" in check["metrics"]
    assert "source_total" in check["metrics"]
    assert "rule_results" not in check["metrics"]


def test_fallback_when_pack_has_no_rules(client):
    """场景包存在但 reconciliation_rules 为空 → 同样回退硬编码口径"""
    asyncio.run(report_pack_service.seed_builtin_packs())
    pack = {
        "id": "T_RECON_EMPTY", "report_name": "无规则测试包", "report_type": "1104",
        "target_table": G11_TARGET, "target_schema": [],
        "source_tables": ["loan_contract"], "reconciliation_rules": [],
        "regulation_keywords": "测试",
    }
    assert asyncio.run(report_pack_service.create_pack(pack)) is not None

    ctx = {"task_id": "TASK_RECON_EMPTY", "tenant_id": "T001",
           "report_pack_id": "T_RECON_EMPTY", "source_tables": ["loan_contract"],
           "target_table": G11_TARGET}
    r = run_agent(ctx, G11_GOOD_SQL)
    check = get_check(r.output, "reconcile")
    assert check["status"] == "pass"
    assert "target_total" in check["metrics"]
    assert "rule_results" not in check["metrics"]


def test_fallback_no_report_pack_id(client):
    """存量任务上下文（无 report_pack_id 且 G01 未种子）→ 回退硬编码，不报错"""
    # 新库中 G01 已种子时走规则驱动也是通过的；此处只验证不缺 pack 字段时校验照常产出
    ctx = {"task_id": "TASK_RECON_LEGACY", "tenant_id": "T001",
           "source_tables": ["loan_contract"], "target_table": G11_TARGET}
    r = run_agent(ctx, G11_GOOD_SQL)
    check = get_check(r.output, "reconcile")
    assert check["status"] == "pass"
