"""
Agent 4: 测试验证Agent (Test Verify)
职责: 在 SQLite 演示数据集上真实执行 Agent 2 生成的转换SQL，并运行 7 类校验脚本，
     产出结构化测试报告。关键项失败（critical_fail）触发编排器回退 Agent 2。

方言声明（L2-D6）：
  生成 SQL 标注为 MySQL 方言，但当前校验执行在 SQLite 演示数据集上进行，
  属于"语法级验证"（SQLite 与 MySQL 在 IFNULL/类型等方面基本兼容，但非完全等价）。
  Docker MySQL 环境可用时，校验执行应切换到 MySQL 路径（见 scripts/seed_mysql.py），
  该切换点预留于 database_mcp 的 db_type 路由。

7 类校验:
  1. 行数校验        目标表行数 > 0，且不大于源表过滤后行数
  2. 非空率校验      关键字段（contract_no/cust_id/loan_balance）非空率 = 100%
  3. 汇总对账        场景包 reconciliation_rules 规则驱动（SQL 片段只读求值 + tolerance 容差），
                     包缺失/无规则时回退硬编码口径：SUM(loan_balance) 与源表按口径重算勾稽
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
            await demo_dataset.aensure_seeded()
            load_error = await self._load_target_table(sql, target_table)

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
                checks = await self._run_all_checks(target_table, task_context)

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
    async def _load_target_table(self, sql: str, target_table: str) -> str:
        """解析 INSERT 语句列清单，建表并执行生成的转换SQL。返回错误信息或空串"""
        try:
            await demo_dataset.adrop_table(target_table)

            # 解析 INSERT INTO t (col1, col2, ...) 的列清单
            match = re.search(r"insert\s+into\s+\w+\s*\(([^)]*)\)", sql, re.IGNORECASE)
            if not match:
                return "生成SQL不是标准的 INSERT INTO ... (列清单) ... SELECT 结构"

            # 剥离行尾注释（-- ...）后再按逗号切分
            col_block = re.sub(r"--[^\n]*", "", match.group(1))
            cols = [c.strip().strip("`") for c in col_block.split(",") if c.strip()]
            col_defs = ", ".join(f'"{c}"' for c in cols)  # SQLite 无类型列，动态接受任意值

            await demo_dataset.aexecute_script([f'CREATE TABLE {target_table} ({col_defs})'])
            await demo_dataset.aexecute_script([sql.rstrip().rstrip(";")])
            return ""
        except Exception as e:
            return str(e)

    # ============================================
    # 7 类校验
    # ============================================
    async def _run_all_checks(self, target_table: str, task_context: dict) -> List[Dict[str, Any]]:
        # 场景包勾稽规则（配置化对账）：包缺失/无规则时回退硬编码口径，绝不阻断校验
        from backend.services import report_pack_service
        pack = await report_pack_service.get_pack_safe(task_context.get("report_pack_id"))
        recon_rules = (pack or {}).get("reconciliation_rules") or []

        return [
            await self._check_row_count(target_table, task_context),
            await self._check_not_null(target_table),
            await self._check_reconcile(target_table, task_context, recon_rules),
            await self._check_duplicates(target_table),
            await self._check_enum_domain(target_table),
            await self._check_overdue_boundary(target_table),
            await self._check_length_truncation(target_table),
        ]

    async def _check_row_count(self, target_table: str, task_context: dict) -> Dict[str, Any]:
        """1. 行数校验：目标表行数>0，且不超过源表有效行数"""
        tgt = await demo_dataset.aquery(f"SELECT COUNT(*) AS cnt FROM {target_table}")
        tgt_cnt = tgt["rows"][0]["cnt"]

        src_cnt = None
        source_tables = task_context.get("source_tables", [])
        if source_tables:
            src = await demo_dataset.aquery(
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

    async def _check_not_null(self, target_table: str) -> Dict[str, Any]:
        """2. 非空率校验：关键字段非空率必须 100%"""
        rates = {}
        bad_samples = []
        for field in self.REQUIRED_FIELDS:
            r = await demo_dataset.aquery(
                f"SELECT COUNT(*) AS total, SUM(CASE WHEN \"{field}\" IS NULL THEN 1 ELSE 0 END) AS nulls "
                f"FROM {target_table}"
            )
            total = r["rows"][0]["total"] or 0
            nulls = r["rows"][0]["nulls"] or 0
            rate = (total - nulls) / total if total else 1.0
            rates[field] = round(rate, 4)
            if rate < 1.0:
                s = await demo_dataset.aquery(
                    f"SELECT contract_no FROM {target_table} WHERE \"{field}\" IS NULL LIMIT 3"
                )
                bad_samples.extend([f"{field} 为 NULL: {row}" for row in s["rows"]])

        passed = all(v >= 1.0 for v in rates.values())
        return self._check_result(
            "not_null", "关键字段非空率", "pass" if passed else "fail",
            metrics={"non_null_rates": rates}, samples=bad_samples, critical=True,
            detail=f"非空率: {rates}"
        )

    # 源表标准过滤口径（与硬编码对账一致的演示口径）
    SOURCE_FILTER = ("is_deleted=0 AND is_test=0 AND org_no='1001' "
                     "AND product_code IN ('P001','P001-G')")
    # 缺省余额口径：贷款余额 = 本金余额 + 资本化利息
    DEFAULT_BALANCE_EXPR = "principal_balance + IFNULL(interest_capitalized, 0)"

    async def _check_reconcile(self, target_table: str, task_context: dict,
                               rules: list = None) -> Dict[str, Any]:
        """3. 汇总对账：
        - 场景包提供了 reconciliation_rules 时，逐条规则在只读通道求值并判定（规则驱动）；
        - 否则回退硬编码口径（目标表总余额 vs 源表按EAST口径重算），保持存量兼容。
        """
        if rules:
            return await self._check_reconcile_by_rules(target_table, task_context, rules)

        tgt = await demo_dataset.aquery(f"SELECT SUM(loan_balance) AS total FROM {target_table}")
        tgt_total = tgt["rows"][0]["total"] or 0

        source_tables = task_context.get("source_tables", [])
        if not source_tables:
            return self._check_result("reconcile", "汇总对账", "skipped",
                                      metrics={}, samples=[], detail="未提供源表，跳过对账")

        src = await demo_dataset.aquery(
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

    # ============================================
    # 规则驱动对账（场景包 reconciliation_rules）
    # ============================================
    async def _check_reconcile_by_rules(self, target_table: str, task_context: dict,
                                        rules: list) -> Dict[str, Any]:
        """逐条执行场景包勾稽规则：任一规则不通过则整项 fail（关键项）"""
        source_tables = task_context.get("source_tables") or ["loan_contract"]
        source_table = source_tables[0]

        results = []
        for rule in rules:
            results.append(await self._eval_rule(rule, target_table, source_table))

        failed = [r for r in results if not r["passed"]]
        return self._check_result(
            "reconcile", "汇总对账(规则驱动)", "pass" if not failed else "fail",
            metrics={"rule_results": results,
                     "rule_count": len(results), "failed_count": len(failed)},
            samples=[f"规则[{r['name']}] 未通过: 实测 {r['actual']} vs 期望 {r['expected']}，"
                     f"差异 {r['abs_diff']}{('，错误: ' + r['error']) if r.get('error') else ''}"
                     for r in failed[:5]],
            critical=True,
            detail=f"{len(results)} 条勾稽规则，{len(failed)} 条未通过"
        )

    async def _eval_rule(self, rule: dict, target_table: str, source_table: str) -> Dict[str, Any]:
        """求值单条勾稽规则，输出 实测值/期望值/差异/是否通过。

        表达式文法（大小写不敏感，均在只读通道执行）：
          SUM(col)                                  → 目标表聚合 vs 源表缺省余额口径
          <聚合片段> = <聚合片段>                    → 左侧在目标表求值，右侧在源表(标准过滤)求值
          SUM_BY(维度, 度量) [= SUM_BY(维度, 表达式)] → 表内/跨表分组勾稽：各组逐组对比
        """
        name = rule.get("name", "未命名规则")
        expression = (rule.get("expression") or "").strip()
        tolerance = float(rule.get("tolerance", 0) or 0)
        result = {"name": name, "expression": expression, "tolerance": tolerance,
                  "actual": None, "expected": None, "abs_diff": None,
                  "rel_diff": None, "passed": False}
        try:
            # 分组勾稽：SUM_BY(dim, expr)
            m = re.match(
                r"(?is)^\s*SUM_BY\(\s*(\w+)\s*,\s*(.+?)\s*\)"
                r"\s*(?:=\s*SUM_BY\(\s*\w+\s*,\s*(.+?)\s*\)\s*)?$",
                expression)
            if m:
                return await self._eval_group_rule(
                    result, m.group(1), m.group(2), m.group(3),
                    target_table, source_table, tolerance)

            # 标量对比：按 = 拆分左右；无 = 时右側取源表缺省余额口径
            if "=" in expression:
                left, right = (p.strip() for p in expression.split("=", 1))
            else:
                left, right = expression, f"SUM({self.DEFAULT_BALANCE_EXPR})"
            actual = await self._eval_scalar(target_table, left)
            expected = await self._eval_scalar(source_table, right, self.SOURCE_FILTER)
            return self._fill_compare(result, actual, expected, tolerance)
        except Exception as e:
            # 规则本身不可执行 → 判不通过并记录错误（不阻断其他规则）
            result["error"] = str(e)
            return result

    async def _eval_scalar(self, table: str, expr: str, where: str = None) -> float:
        """在只读通道执行标量聚合片段（如 SUM(loan_balance) / COUNT(*)）"""
        sql = f"SELECT {expr} AS v FROM {table}" + (f" WHERE {where}" if where else "")
        rows = (await demo_dataset.aquery(sql))["rows"]
        return float(rows[0]["v"] or 0) if rows else 0.0

    def _fill_compare(self, result: dict, actual: float, expected: float,
                      tolerance: float) -> Dict[str, Any]:
        """容差判定：绝对差落在 tolerance 内即通过（rel_diff 仅作展示指标）"""
        diff = abs(actual - expected)
        rel = diff / abs(expected) if expected else 0.0
        # 差值先按 6 位小数舍入再与容差比较，消除浮点求和毛刺（如 0.0050000000001）
        rounded = round(diff, 6)
        result.update(actual=round(actual, 4), expected=round(expected, 4),
                      abs_diff=rounded, rel_diff=round(rel, 8),
                      passed=rounded <= tolerance)
        return result

    async def _eval_group_rule(self, result: dict, dim: str, t_expr: str, s_expr,
                               target_table: str, source_table: str,
                               tolerance: float) -> Dict[str, Any]:
        """分组勾稽：目标表按维度分组的聚合 vs 源表同维度分组重算，逐组对比"""
        s_expr = s_expr or self.DEFAULT_BALANCE_EXPR
        t_rows = (await demo_dataset.aquery(
            f'SELECT "{dim}" AS g, SUM({t_expr}) AS v FROM {target_table} GROUP BY "{dim}"'
        ))["rows"]
        s_rows = (await demo_dataset.aquery(
            f'SELECT "{dim}" AS g, SUM({s_expr}) AS v FROM {source_table} '
            f'WHERE {self.SOURCE_FILTER} GROUP BY "{dim}"'
        ))["rows"]

        t_map = {str(r["g"]): float(r["v"] or 0) for r in t_rows}
        s_map = {str(r["g"]): float(r["v"] or 0) for r in s_rows}
        groups = {}
        worst_diff = 0.0
        passed = True
        for g in sorted(set(t_map) | set(s_map)):
            a, e = t_map.get(g, 0.0), s_map.get(g, 0.0)
            d = abs(a - e)
            ok = round(d, 6) <= tolerance
            passed = passed and ok
            worst_diff = max(worst_diff, d)
            groups[g] = {"actual": round(a, 4), "expected": round(e, 4),
                         "abs_diff": round(d, 6), "passed": ok}

        result.update(actual=groups, expected="见各分组期望值", abs_diff=round(worst_diff, 6),
                      group_count=len(groups), passed=passed)
        return result

    async def _check_duplicates(self, target_table: str) -> Dict[str, Any]:
        """4. 重复记录检测：contract_no 主键唯一"""
        r = await demo_dataset.aquery(
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

    async def _check_enum_domain(self, target_table: str) -> Dict[str, Any]:
        """5. 枚举值域校验：five_classify ∈ 1-5（目标表无此列则跳过）"""
        cols = (await demo_dataset.aquery(f"SELECT * FROM {target_table} LIMIT 1"))["columns"]
        if "five_classify" not in cols:
            return self._check_result(
                "enum_domain", "枚举值域校验", "skipped",
                metrics={}, samples=[],
                detail="目标表未包含 five_classify 字段，跳过枚举校验"
            )
        r = await demo_dataset.aquery(
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

    async def _check_overdue_boundary(self, target_table: str) -> Dict[str, Any]:
        """6. 阈值边界抽查：逾期 90 天分界规则落实"""
        cols = (await demo_dataset.aquery(f"SELECT * FROM {target_table} LIMIT 1"))["columns"]
        if "overdue_principal" not in cols:
            return self._check_result(
                "overdue_boundary", "逾期90天边界抽查", "skipped",
                metrics={}, samples=[],
                detail="目标表未包含 overdue_principal 字段，跳过边界抽查"
            )

        # 目标表与源表按 contract_no 关联，验证边界规则
        r = await demo_dataset.aquery(
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

    async def _check_length_truncation(self, target_table: str) -> Dict[str, Any]:
        """7. 类型/长度截断检测：contract_no ≤ 32 字符"""
        r = await demo_dataset.aquery(
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
