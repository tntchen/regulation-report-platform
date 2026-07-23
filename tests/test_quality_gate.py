"""
质量校验Agent（QualityGate）六维判定纯函数单测（L2-Day10 补齐）
不依赖数据库 / AI 服务，直接实例化 Agent 调各维度私有方法，
覆盖六维各自的正（放行）反（拦截/告警）用例与门禁汇总逻辑。

运行方式: python -m pytest tests/test_quality_gate.py -v
"""

import pytest

from backend.agents.quality_gate import QualityGateAgent


@pytest.fixture(scope="module")
def agent():
    return QualityGateAgent()


def _levels(dim_result):
    return {i["level"] for i in dim_result["issues"]}


# 构造一个"全合规"基准 SQL，各用例在此基础上破坏单一维度
GOOD_SQL = """
INSERT INTO rpt_east_housing_loan (contract_no, loan_balance, execute_rate, biz_date, org_no)
SELECT contract_no,
       ROUND(principal_balance + IFNULL(interest_capitalized, 0), 4) AS loan_balance,
       ROUND(execute_rate, 6) AS execute_rate,
       biz_date, org_no
FROM loan_contract
WHERE is_deleted = 0 AND is_test = 0 AND org_no = '1001' AND biz_date = '2024-12-31'
"""

SCHEMA = {
    "loan_contract": {
        "columns": [
            {"column_name": "contract_no", "data_type": "VARCHAR", "is_nullable": "NO", "is_pk": True},
            {"column_name": "principal_balance", "data_type": "DECIMAL(20,4)", "is_nullable": "NO"},
            {"column_name": "interest_capitalized", "data_type": "DECIMAL(20,4)", "is_nullable": "YES"},
            {"column_name": "execute_rate", "data_type": "DECIMAL(20,6)", "is_nullable": "YES"},
            {"column_name": "biz_date", "data_type": "DATE", "is_nullable": "NO", "is_index": True},
            {"column_name": "org_no", "data_type": "VARCHAR", "is_nullable": "NO", "is_index": True},
        ]
    }
}


# ============================================
# 维度1: 口径合规
# ============================================

class TestCaliber:
    def test_blocker_when_balance_missing_interest(self, agent):
        """本金+贷款余额同时出现但不含资本化利息 → blocker"""
        sql = "SELECT principal_balance AS loan_balance FROM t WHERE is_deleted=0"
        r = agent._check_caliber(sql, [], {})
        assert r["status"] == "blocker"
        assert any("interest_capitalized" in i["suggestion"] for i in r["issues"])

    def test_pass_when_interest_included(self, agent):
        """余额含 interest_capitalized → 不触发该 blocker"""
        r = agent._check_caliber(
            "SELECT principal_balance + IFNULL(interest_capitalized,0) AS loan_balance FROM t", [], {})
        assert not any(i["level"] == "blocker" for i in r["issues"])

    def test_no_balance_columns_no_issue(self, agent):
        """不涉及余额字段 → 口径维度无 blocker"""
        r = agent._check_caliber("SELECT contract_no FROM t", [], {})
        assert r["status"] == "pass"

    def test_warning_combo_loan_trap(self, agent):
        """critical 陷阱提示公积金组合贷，SQL 未含 P001-G → warning"""
        traps = [{"level": "critical", "description": "公积金组合贷(P001-G)需纳入统计"}]
        r = agent._check_caliber("SELECT product_code FROM t WHERE product_code='P001'", traps, {})
        assert any(i["level"] == "warning" and "P001-G" in i["message"] for i in r["issues"])

    def test_no_warning_when_combo_loan_included(self, agent):
        """SQL 已含 P001-G → 该陷阱不报"""
        traps = [{"level": "critical", "description": "公积金组合贷(P001-G)需纳入统计"}]
        r = agent._check_caliber("SELECT * FROM t WHERE product_code IN ('P001','P001-G')", traps, {})
        assert not any("P001-G" in i["message"] for i in r["issues"])

    def test_non_critical_trap_ignored(self, agent):
        """非 critical 陷阱不触发检查"""
        traps = [{"level": "info", "description": "公积金组合贷需纳入统计"}]
        r = agent._check_caliber("SELECT product_code FROM t", traps, {})
        assert r["status"] == "pass"


# ============================================
# 维度2: 类型安全
# ============================================

class TestTypeSafety:
    def test_warning_decimal_without_round(self, agent):
        """金额字段未 ROUND → warning"""
        sql = "SELECT principal_balance FROM t WHERE org_no='1'"
        r = agent._check_type_safety(sql, SCHEMA)
        assert any(i["level"] == "warning" and "ROUND" in i["suggestion"] for i in r["issues"])

    def test_blocker_rate_not_round6(self, agent):
        """利率字段未按 D20.6 处理 → blocker"""
        sql = "SELECT ROUND(execute_rate, 4) FROM t"
        r = agent._check_type_safety(sql, SCHEMA)
        assert any(i["level"] == "blocker" and "D20.6" in i["message"] for i in r["issues"])

    def test_pass_rate_round6(self, agent):
        """利率 ROUND 6 位 + 金额 ROUND → 通过"""
        sql = "SELECT ROUND(principal_balance, 4), ROUND(execute_rate, 6) FROM t"
        r = agent._check_type_safety(sql, SCHEMA)
        assert r["status"] == "pass"

    def test_pass_nested_ifnull_round6(self, agent):
        """ROUND(IFNULL(execute_rate,0),6) 一层嵌套括号也应识别为合规"""
        sql = "SELECT ROUND(IFNULL(execute_rate, 0), 6) FROM t"
        r = agent._check_type_safety(sql, SCHEMA)
        assert not any(i["level"] == "blocker" for i in r["issues"])

    def test_empty_schema_no_issue(self, agent):
        """无 schema 信息时不误报"""
        r = agent._check_type_safety("SELECT a FROM t", {})
        assert r["status"] == "pass"


# ============================================
# 维度3: 空值防御
# ============================================

class TestNullDefense:
    def test_blocker_nullable_in_arithmetic(self, agent):
        """可空字段参与加减乘除且无防御 → blocker"""
        sql = "SELECT principal_balance + interest_capitalized FROM t"
        r = agent._check_null_defense(sql, SCHEMA)
        assert r["status"] == "blocker"
        assert "interest_capitalized" in r["issues"][0]["message"]

    def test_pass_with_ifnull(self, agent):
        """IFNULL 包装后通过"""
        sql = "SELECT principal_balance + IFNULL(interest_capitalized, 0) FROM t"
        r = agent._check_null_defense(sql, SCHEMA)
        assert r["status"] == "pass"

    def test_pass_with_coalesce(self, agent):
        """COALESCE 包装后通过"""
        sql = "SELECT COALESCE(execute_rate, 0) * 100 FROM t"
        r = agent._check_null_defense(sql, SCHEMA)
        assert r["status"] == "pass"

    def test_pass_when_nullable_not_in_expression(self, agent):
        """可空字段仅裸列输出（不参与运算/CASE）不触发"""
        sql = "SELECT execute_rate FROM t WHERE org_no = '1'"
        r = agent._check_null_defense(sql, SCHEMA)
        assert r["status"] == "pass"


# ============================================
# 维度4: 性能友好
# ============================================

class TestPerformance:
    def test_blocker_select_star(self, agent):
        r = agent._check_performance("SELECT * FROM t WHERE id=1", SCHEMA)
        assert any(i["level"] == "blocker" and "SELECT *" in i["message"] for i in r["issues"])

    def test_warning_no_where(self, agent):
        r = agent._check_performance("SELECT contract_no FROM t", SCHEMA)
        assert any(i["level"] == "warning" and "WHERE" in i["message"] for i in r["issues"])

    def test_warning_function_on_indexed_col(self, agent):
        """WHERE 中对索引列套函数 → 索引失效 warning"""
        sql = "SELECT contract_no FROM t WHERE DATE_FORMAT(biz_date, '%Y') = '2024'"
        r = agent._check_performance(sql, SCHEMA)
        assert any(i["level"] == "warning" and "biz_date" in i["message"] for i in r["issues"])

    def test_pass_bare_index_compare(self, agent):
        """索引列裸列比较 + 显式字段 → 通过"""
        sql = "SELECT contract_no FROM t WHERE biz_date = '2024-12-31' AND org_no = '1'"
        r = agent._check_performance(sql, SCHEMA)
        assert r["status"] == "pass"


# ============================================
# 维度5: 安全合规
# ============================================

class TestSecurity:
    @pytest.mark.parametrize("keyword", ["DROP", "TRUNCATE", "ALTER", "GRANT", "DELETE FROM"])
    def test_blocker_dangerous_keywords(self, agent, keyword):
        r = agent._check_security(f"{keyword} something is_deleted is_test org_no")
        assert any(i["level"] == "blocker" and keyword in i["message"] for i in r["issues"])

    def test_blocker_missing_filters(self, agent):
        """缺 is_deleted / is_test / org_no 三个过滤 → 3 个 blocker"""
        r = agent._check_security("SELECT contract_no FROM t")
        blockers = [i for i in r["issues"] if i["level"] == "blocker"]
        assert len(blockers) == 3
        msgs = " ".join(i["message"] for i in blockers)
        assert "is_deleted" in msgs and "is_test" in msgs and "org_no" in msgs

    def test_warning_plain_sensitive_column(self, agent):
        """C2 敏感字段明文输出 → warning"""
        sql = "SELECT id_card FROM t WHERE is_deleted=0 AND is_test=0 AND org_no='1'"
        r = agent._check_security(sql)
        assert any(i["level"] == "warning" and "id_card" in i["message"] for i in r["issues"])

    def test_no_warning_masked_sensitive_column(self, agent):
        """敏感字段经 CONCAT/SUBSTR 脱敏后不报"""
        sql = ("SELECT CONCAT(SUBSTR(id_card,1,3),'****',SUBSTR(id_card,-4)) "
               "FROM t WHERE is_deleted=0 AND is_test=0 AND org_no='1'")
        r = agent._check_security(sql)
        assert not any("id_card" in i["message"] for i in r["issues"])

    def test_pass_compliant_sql(self, agent):
        r = agent._check_security(GOOD_SQL)
        assert r["status"] == "pass"


# ============================================
# 维度6: 监管特殊
# ============================================

class TestRegulatory:
    def test_blocker_overdue_without_90_boundary(self, agent):
        r = agent._check_regulatory("SELECT overdue_days FROM t", {})
        assert any(i["level"] == "blocker" and "90天" in i["message"] for i in r["issues"])

    @pytest.mark.parametrize("expr", [">= 91", ">90", "> 90"])
    def test_pass_overdue_with_boundary(self, agent, expr):
        sql = f"SELECT CASE WHEN overdue_days {expr} THEN 1 ELSE 0 END FROM t"
        r = agent._check_regulatory(sql, {})
        assert r["status"] == "pass"

    def test_warning_rate_report_without_lpr(self, agent):
        ctx = {"report_type": "利率报备", "report_code": "RATE_01"}
        r = agent._check_regulatory("SELECT execute_rate FROM t", ctx)
        assert any(i["level"] == "warning" and "LPR" in i["message"] for i in r["issues"])

    def test_pass_rate_report_with_lpr(self, agent):
        ctx = {"report_type": "利率报备", "report_code": "RATE_01"}
        r = agent._check_regulatory("SELECT execute_rate - lpr FROM t", ctx)
        assert r["status"] == "pass"

    def test_warning_p001_without_combo(self, agent):
        sql = "SELECT * FROM t WHERE product_code = 'P001'"
        r = agent._check_regulatory(sql, {})
        assert any(i["level"] == "warning" and "P001-G" in i["message"] for i in r["issues"])

    def test_pass_p001_with_combo(self, agent):
        sql = "SELECT * FROM t WHERE product_code IN ('P001','P001-G')"
        r = agent._check_regulatory(sql, {})
        assert r["status"] == "pass"


# ============================================
# 汇总逻辑与 execute 集成
# ============================================

class TestAggregation:
    def test_dimension_result_precedence(self, agent):
        """blocker > warning > pass"""
        assert agent._dimension_result([])["status"] == "pass"
        assert agent._dimension_result([{"level": "warning"}])["status"] == "warning"
        assert agent._dimension_result(
            [{"level": "warning"}, {"level": "blocker"}])["status"] == "blocker"

    @pytest.mark.asyncio
    async def test_execute_empty_sql_blocks(self, agent):
        """Agent 2 无产出 → 直接 block"""
        r = await agent.execute({}, codegen_output={"generated_code": ""})
        assert r.status == "success"
        assert r.output["gate_result"] == "block"
        assert r.output["blocker_count"] == 1

    @pytest.mark.asyncio
    async def test_execute_good_sql_pass(self, agent):
        """全合规 SQL → gate pass"""
        r = await agent.execute(
            {},
            codegen_output={"generated_code": GOOD_SQL, "source_schemas": SCHEMA},
            regulation_output={"traps_identified": []},
        )
        assert r.output["gate_result"] == "pass"
        assert r.output["blocker_count"] == 0
        assert r.output["warning_count"] == 0
        assert "全部通过" in r.output["summary"]

    @pytest.mark.asyncio
    async def test_execute_blocker_and_warning_counts(self, agent):
        """混合问题：blocker 计数与 auto_fix_suggestions 汇总正确"""
        bad_sql = ("SELECT principal_balance AS loan_balance, id_card, execute_rate "
                   "FROM loan_contract")  # 缺三个过滤(3 blocker) + 口径(1 blocker) + 利率(1 blocker) + 无WHERE(1 warning) + 金额未ROUND(1 warning) + 明文id_card(1 warning)
        r = await agent.execute(
            {},
            codegen_output={"generated_code": bad_sql, "source_schemas": SCHEMA},
            regulation_output={"traps_identified": []},
        )
        out = r.output
        assert out["gate_result"] == "block"
        assert out["blocker_count"] >= 4
        assert out["warning_count"] >= 1
        assert len(out["auto_fix_suggestions"]) == len(out["issues"])
        # 每条建议都带维度前缀
        assert all(s.startswith("[") for s in out["auto_fix_suggestions"])
