"""
Agent 4: 测试验证Agent (Test Verify)
职责: 在 SQLite 演示数据集上真实执行 Agent 2 生成的转换SQL，并运行 7 类校验脚本，
     产出结构化测试报告。关键项失败（critical_fail）触发编排器回退 Agent 2。

7 类校验:
  1. 行数校验        目标表行数 > 0，且不大于源表过滤后行数
  2. 非空率校验      关键字段（contract_no/cust_id/loan_balance）非空率 = 100%
  3. 汇总对账        SUM(loan_balance) 与源表按口径重算的总余额勾稽一致
  4. 重复记录检测    主键 contract_no 无重复
  5. 枚举值域校验    five_classify 等枚举字段取值合法（目标表无该列则 skipped）
  6. 阈值边界抽查    逾期 90 天分界规则：od>=91 整笔本金，od=0 逾期本金为 0
  7. 类型/长度截断   字符串字段长度未超目标定义（如 contract_no<=32）
"""

import re
import time
from typing import Dict, Any, List
from backend.agents.base import BaseAgent, AgentResult
from backend.mcp.demo_dataset import demo_dataset


class TestVerifyAgent(BaseAgent):
    """测试验证Agent"""

    # 关键字段（非空率必须为 100%）
    REQUIRED_FIELDS = ["contract_no", "cust_id", "loan_balance"]

    # 对账允许的最大相对误差（浮点误差容忍）
    RECONCILE_TOLERANCE = 0.0001

    def __init__(self):
        super().__init__(
            name="test_verify",
            description="在演示数据集上真实执行生成SQL并运行7类校验脚本"
        )

    async def execute(self, task_context: dict, codegen_output: dict = None,
                      regulation_output: dict = None, **kwargs) -> AgentResult:
        start_time = time.time()

        try:
            codegen_output = codegen_output or {}
            sql = codegen_output.get("generated_code", "")
            target_table = task_context.get("target_table", "rpt_result")

            # 无代码可验证 → 关键失败
            if not sql:
                return AgentResult(
                    agent_name=self.name,
                    status="success",
                    output={
                        "overall_result": "fail",
                        "critical_fail": True,
                        "fail_reasons": ["Agent 2 未产出可执行代码"],
                        "checks": [],
                        "summary": "测试验证失败：无生成代码"
                    },
                    duration_ms=int((time.time() - start_time) * 1000)
                )

            # 准备演示数据集 + 装载目标表
            demo_dataset.ensure_seeded()
            load_error = self._load_target_table(sql, target_table)

            checks = []
            if load_error:
                # SQL 无法执行属于关键失败
                checks.append(self._check_result(
                    "sql_executable", "生成SQL可执行性", "fail",
                    metrics={"error": load_error},
                    samples=[load_error],
                    critical=True
                ))
            else:
                checks = self._run_all_checks(target_table, task_context)

            # 汇总
            failed = [c for c in checks if c["status"] == "fail"]
            critical_failed = [c for c in failed if c.get("critical")]
            overall = "fail" if failed else "pass"

            output = {
                "overall_result": overall,
                "critical_fail": bool(critical_failed),
                "fail_reasons": [f"[{c['name']}] {c['detail']}" for c in critical_failed],
                "checks": checks,
                "pass_count": sum(1 for c in checks if c["status"] == "pass"),
                "fail_count": len(failed),
                "skipped_count": sum(1 for c in checks if c["status"] == "skipped"),
                "summary": f"测试验证{('通过' if overall == 'pass' else '失败')}："
                           f"{sum(1 for c in checks if c['status'] == 'pass')} 通过 / "
                           f"{len(failed)} 失败 / {sum(1 for c in checks if c['status'] == 'skipped')} 跳过"
            }

            return AgentResult(
                agent_name=self.name,
                status="success",
                output=output,
                duration_ms=int((time.time() - start_time) * 1000)
            )

        except Exception as e:
            return AgentResult(
                agent_name=self.name,
                status="failed",
                output={"overall_result": "fail", "critical_fail": True,
                        "fail_reasons": [str(e)], "checks": []},
                error=str(e),
                duration_ms=int((time.time() - start_time) * 1000)
            )

    # ============================================
    # 目标表装载
    # ============================================
    def _load_target_table(self, sql: str, target_table: str) -> str:
        """解析 INSERT 语句列清单，建表并执行生成的转换SQL。返回错误信息或空串"""
        try:
            demo_dataset.drop_table(target_table)

            # 解析 INSERT INTO t (col1, col2, ...) 的列清单
            match = re.search(r"insert\s+into\s+\w+\s*\(([^)]*)\)", sql, re.IGNORECASE)
            if not match:
                return "生成SQL不是标准的 INSERT INTO ... (列清单) ... SELECT 结构"

            # 剥离行尾注释（-- ...）后再按逗号切分
            col_block = re.sub(r"--[^\n]*", "", match.group(1))
            cols = [c.strip().strip("`") for c in col_block.split(",") if c.strip()]
            col_defs = ", ".join(f'"{c}"' for c in cols)  # SQLite 无类型列，动态接受任意值

            demo_dataset.execute_script([f'CREATE TABLE {target_table} ({col_defs})'])
            demo_dataset.execute_script([sql.rstrip().rstrip(";")])
            return ""
        except Exception as e:
            return str(e)

    # ============================================
    # 7 类校验
    # ============================================
    def _run_all_checks(self, target_table: str, task_context: dict) -> List[Dict[str, Any]]:
        return [
            self._check_row_count(target_table, task_context),
            self._check_not_null(target_table),
            self._check_reconcile(target_table, task_context),
            self._check_duplicates(target_table),
            self._check_enum_domain(target_table),
            self._check_overdue_boundary(target_table),
            self._check_length_truncation(target_table),
        ]

    def _check_row_count(self, target_table: str, task_context: dict) -> Dict[str, Any]:
        """1. 行数校验：目标表行数>0，且不超过源表有效行数"""
        tgt = demo_dataset.query(f"SELECT COUNT(*) AS cnt FROM {target_table}")
        tgt_cnt = tgt["rows"][0]["cnt"]

        src_cnt = None
        source_tables = task_context.get("source_tables", [])
        if source_tables:
            src = demo_dataset.query(
                f"SELECT COUNT(*) AS cnt FROM {source_tables[0]} "
                f"WHERE is_deleted=0 AND is_test=0 AND org_no='1001' "
                f"AND product_code IN ('P001','P001-G')"
            )
            src_cnt = src["rows"][0]["cnt"]

        passed = tgt_cnt > 0 and (src_cnt is None or tgt_cnt <= src_cnt)
        return self._check_result(
            "row_count", "行数校验", "pass" if passed else "fail",
            metrics={"target_rows": tgt_cnt, "source_eligible_rows": src_cnt},
            samples=[] if passed else [f"目标表行数 {tgt_cnt} 异常（源表有效行数 {src_cnt}）"],
            critical=True,
            detail=f"目标表 {tgt_cnt} 行，源表有效行 {src_cnt} 行"
        )

    def _check_not_null(self, target_table: str) -> Dict[str, Any]:
        """2. 非空率校验：关键字段非空率必须 100%"""
        rates = {}
        bad_samples = []
        for field in self.REQUIRED_FIELDS:
            r = demo_dataset.query(
                f"SELECT COUNT(*) AS total, SUM(CASE WHEN \"{field}\" IS NULL THEN 1 ELSE 0 END) AS nulls "
                f"FROM {target_table}"
            )
            total = r["rows"][0]["total"] or 0
            nulls = r["rows"][0]["nulls"] or 0
            rate = (total - nulls) / total if total else 1.0
            rates[field] = round(rate, 4)
            if rate < 1.0:
                s = demo_dataset.query(
                    f"SELECT contract_no FROM {target_table} WHERE \"{field}\" IS NULL LIMIT 3"
                )
                bad_samples.extend([f"{field} 为 NULL: {row}" for row in s["rows"]])

        passed = all(v >= 1.0 for v in rates.values())
        return self._check_result(
            "not_null", "关键字段非空率", "pass" if passed else "fail",
            metrics={"non_null_rates": rates}, samples=bad_samples, critical=True,
            detail=f"非空率: {rates}"
        )

    def _check_reconcile(self, target_table: str, task_context: dict) -> Dict[str, Any]:
        """3. 汇总对账：目标表总余额与源表按EAST口径重算值勾稽"""
        tgt = demo_dataset.query(f"SELECT SUM(loan_balance) AS total FROM {target_table}")
        tgt_total = tgt["rows"][0]["total"] or 0

        source_tables = task_context.get("source_tables", [])
        if not source_tables:
            return self._check_result("reconcile", "汇总对账", "skipped",
                                      metrics={}, samples=[], detail="未提供源表，跳过对账")

        src = demo_dataset.query(
            f"SELECT ROUND(SUM(principal_balance + IFNULL(interest_capitalized, 0)), 4) AS total "
            f"FROM {source_tables[0]} "
            f"WHERE is_deleted=0 AND is_test=0 AND org_no='1001' "
            f"AND product_code IN ('P001','P001-G')"
        )
        src_total = src["rows"][0]["total"] or 0

        diff = abs((tgt_total or 0) - (src_total or 0))
        rel = diff / src_total if src_total else 0
        passed = rel <= self.RECONCILE_TOLERANCE
        return self._check_result(
            "reconcile", "汇总对账(总额勾稽)", "pass" if passed else "fail",
            metrics={"target_total": tgt_total, "source_total": src_total,
                     "abs_diff": round(diff, 4), "rel_diff": round(rel, 8)},
            samples=[] if passed else [f"目标总额 {tgt_total} vs 源表重算 {src_total}，差异 {diff}"],
            critical=True,
            detail=f"目标 {tgt_total} vs 源表 {src_total}，相对差异 {rel:.6%}"
        )

    def _check_duplicates(self, target_table: str) -> Dict[str, Any]:
        """4. 重复记录检测：contract_no 主键唯一"""
        r = demo_dataset.query(
            f"SELECT contract_no, COUNT(*) AS cnt FROM {target_table} "
            f"GROUP BY contract_no HAVING COUNT(*) > 1 LIMIT 5"
        )
        dups = r["rows"]
        passed = len(dups) == 0
        return self._check_result(
            "duplicates", "重复记录检测", "pass" if passed else "fail",
            metrics={"duplicate_keys": len(dups)},
            samples=[f"重复主键 {d['contract_no']} 出现 {d['cnt']} 次" for d in dups],
            critical=True,
            detail=f"重复主键 {len(dups)} 个"
        )

    def _check_enum_domain(self, target_table: str) -> Dict[str, Any]:
        """5. 枚举值域校验：five_classify ∈ 1-5（目标表无此列则跳过）"""
        cols = demo_dataset.query(f"SELECT * FROM {target_table} LIMIT 1")["columns"]
        if "five_classify" not in cols:
            return self._check_result(
                "enum_domain", "枚举值域校验", "skipped",
                metrics={}, samples=[],
                detail="目标表未包含 five_classify 字段，跳过枚举校验"
            )
        r = demo_dataset.query(
            f"SELECT five_classify, COUNT(*) AS cnt FROM {target_table} "
            f"WHERE five_classify NOT IN ('1','2','3','4','5') GROUP BY five_classify LIMIT 5"
        )
        bad = r["rows"]
        return self._check_result(
            "enum_domain", "枚举值域校验", "pass" if not bad else "fail",
            metrics={"invalid_count": sum(b["cnt"] for b in bad)},
            samples=[f"非法枚举值: {b['five_classify']}" for b in bad],
            detail=f"非法枚举 {len(bad)} 类"
        )

    def _check_overdue_boundary(self, target_table: str) -> Dict[str, Any]:
        """6. 阈值边界抽查：逾期 90 天分界规则落实"""
        cols = demo_dataset.query(f"SELECT * FROM {target_table} LIMIT 1")["columns"]
        if "overdue_principal" not in cols:
            return self._check_result(
                "overdue_boundary", "逾期90天边界抽查", "skipped",
                metrics={}, samples=[],
                detail="目标表未包含 overdue_principal 字段，跳过边界抽查"
            )

        # 目标表与源表按 contract_no 关联，验证边界规则
        r = demo_dataset.query(
            f"SELECT t.contract_no, s.overdue_days, t.overdue_principal, s.principal_balance "
            f"FROM {target_table} t JOIN loan_contract s ON t.contract_no = s.contract_no"
        )
        violations = []
        checked = 0
        for row in r["rows"]:
            od = row["overdue_days"] or 0
            op = row["overdue_principal"] or 0
            pb = row["principal_balance"] or 0
            checked += 1
            if od == 0 and abs(op) > 0.0001:
                violations.append(f"{row['contract_no']}: 未逾期但逾期本金={op}")
            elif od > 0 and abs(op - pb) > 0.01:
                violations.append(f"{row['contract_no']}: 逾期{od}天，逾期本金 {op} ≠ 本金 {pb}")

        passed = not violations
        return self._check_result(
            "overdue_boundary", "逾期90天边界抽查", "pass" if passed else "fail",
            metrics={"checked_rows": checked, "violations": len(violations)},
            samples=violations[:5],
            detail=f"抽查 {checked} 行，违规 {len(violations)} 行"
        )

    def _check_length_truncation(self, target_table: str) -> Dict[str, Any]:
        """7. 类型/长度截断检测：contract_no ≤ 32 字符"""
        r = demo_dataset.query(
            f"SELECT contract_no, LENGTH(contract_no) AS len FROM {target_table} "
            f"WHERE LENGTH(contract_no) > 32 LIMIT 5"
        )
        over = r["rows"]
        passed = not over
        return self._check_result(
            "length_truncation", "字段长度截断检测", "pass" if passed else "fail",
            metrics={"overlength_count": len(over)},
            samples=[f"{o['contract_no']} 长度 {o['len']}" for o in over],
            detail=f"超长记录 {len(over)} 条"
        )

    # ============================================
    # 工具方法
    # ============================================
    def _check_result(self, check_id: str, name: str, status: str,
                      metrics: dict, samples: list, critical: bool = False,
                      detail: str = "") -> Dict[str, Any]:
        """构造单项校验结果"""
        return {
            "check_id": check_id,
            "name": name,
            "status": status,          # pass/fail/skipped
            "critical": critical,      # 是否为关键项（fail 触发门禁回退）
            "metrics": metrics,
            "samples": samples,
            "detail": detail
        }
