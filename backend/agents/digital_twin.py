"""
Agent 5: 数字孪生Agent (Digital Twin)
职责: 同一批源数据，用两种监管口径模拟转换并做差异分析。

Demo 场景（设计文档 §四 场景1）:
  口径A（1104 G01）: 个人住房贷款余额 = 纯本金余额（principal_balance）
  口径B（EAST）:     贷款余额 = 账面余额 = 本金 + 资本化利息（interest_capitalized）

产出结构化对比报告:
  - 两口径汇总结果（记录数、总额）
  - 差异量化（差异总额、差异率、差异记录数、差异等级分布）
  - Top 差异样例（逐笔：两口径数值、绝对/相对差异、等级）
  - 归因说明（差异方向、量级、制度依据、建议）
"""

import time
from typing import Dict, Any, List
from backend.agents.base import BaseAgent, AgentResult
from backend.mcp.demo_dataset import demo_dataset


class DigitalTwinAgent(BaseAgent):
    """数字孪生Agent"""

    # 差异等级阈值（设计文档 §3.3，贷款余额指标）
    THRESHOLDS = {"critical": 0.05, "high": 0.02, "medium": 0.005}

    def __init__(self):
        super().__init__(
            name="digital_twin",
            description="1104 vs EAST 双口径模拟转换与差异归因分析"
        )

    async def execute(self, task_context: dict, **kwargs) -> AgentResult:
        start_time = time.time()

        try:
            await demo_dataset.aensure_seeded()
            source_tables = task_context.get("source_tables", ["loan_contract"])
            source = source_tables[0] if source_tables else "loan_contract"

            # 统一过滤条件：与报送口径一致（住房贷款、有效数据、本机构）
            where = ("is_deleted=0 AND is_test=0 AND org_no='1001' "
                     "AND product_code IN ('P001','P001-G')")

            # 实例A：1104 G01 口径（纯本金余额）
            instance_a = await demo_dataset.aquery(f"""
                SELECT contract_no,
                       ROUND(principal_balance, 4) AS balance
                FROM {source} WHERE {where}
            """)

            # 实例B：EAST 口径（账面余额 = 本金 + 资本化利息）
            instance_b = await demo_dataset.aquery(f"""
                SELECT contract_no,
                       ROUND(principal_balance + IFNULL(interest_capitalized, 0), 4) AS balance,
                       IFNULL(interest_capitalized, 0) AS interest_part
                FROM {source} WHERE {where}
            """)

            # 逐笔差异分析（按 contract_no 键比对）
            diffs = self._analyze_diff(instance_a["rows"], instance_b["rows"])

            # 汇总指标
            total_a = round(sum(r["balance"] for r in instance_a["rows"]), 4)
            total_b = round(sum(r["balance"] for r in instance_b["rows"]), 4)
            abs_diff_total = round(total_b - total_a, 4)
            rel_diff_total = abs_diff_total / total_a if total_a else 0

            level_dist = {}
            for d in diffs["records"]:
                level_dist[d["diff_level"]] = level_dist.get(d["diff_level"], 0) + 1

            # 归因说明
            attribution = self._build_attribution(
                total_a, total_b, abs_diff_total, rel_diff_total, diffs
            )

            duration_ms = int((time.time() - start_time) * 1000)

            return AgentResult(
                agent_name=self.name,
                status="success",
                output={
                    "scenario": "1104_G01 vs EAST 贷款余额口径对比",
                    "instance_a": {
                        "name": "1104 G01 个人住房贷款余额",
                        "caliber": "纯本金余额（principal_balance）",
                        "record_count": len(instance_a["rows"]),
                        "total_balance": total_a
                    },
                    "instance_b": {
                        "name": "EAST 借据表贷款余额",
                        "caliber": "账面余额 = 本金 + 资本化利息",
                        "record_count": len(instance_b["rows"]),
                        "total_balance": total_b
                    },
                    "diff_analysis": {
                        "abs_diff_total": abs_diff_total,
                        "rel_diff_total": round(rel_diff_total, 6),
                        "diff_record_count": diffs["diff_count"],
                        "match_record_count": diffs["match_count"],
                        "level_distribution": level_dist,
                        "top_diff_samples": diffs["records"][:5]
                    },
                    "attribution": attribution,
                    "summary": (f"两口径差异总额 {abs_diff_total:,.2f} 元"
                                f"（相对差异 {rel_diff_total:.4%}），"
                                f"{diffs['diff_count']} 笔存在差异：{attribution['conclusion']}")
                },
                duration_ms=duration_ms
            )

        except Exception as e:
            return AgentResult(
                agent_name=self.name,
                status="failed",
                output={},
                error=str(e),
                duration_ms=int((time.time() - start_time) * 1000)
            )

    # ============================================
    # 差异分析引擎（设计文档 §3.3 简化版）
    # ============================================
    def _analyze_diff(self, rows_a: List[dict], rows_b: List[dict]) -> Dict[str, Any]:
        """逐行比对两个实例，输出差异记录与统计"""
        map_a = {r["contract_no"]: r["balance"] for r in rows_a}
        map_b = {r["contract_no"]: r["balance"] for r in rows_b}

        records = []
        match_count = 0

        for key in sorted(set(map_a) | set(map_b)):
            va = map_a.get(key)
            vb = map_b.get(key)

            if va is None or vb is None:
                # 单边缺失 → critical
                records.append({
                    "contract_no": key,
                    "value_1104": va,
                    "value_east": vb,
                    "abs_diff": None,
                    "rel_diff": None,
                    "diff_level": "critical",
                    "note": "单边缺失：某一口径下该笔不存在"
                })
                continue

            abs_diff = round(vb - va, 4)
            rel_diff = abs(abs_diff) / max(abs(va), abs(vb)) if max(abs(va), abs(vb)) else 0

            if abs_diff == 0:
                match_count += 1
                continue

            records.append({
                "contract_no": key,
                "value_1104": va,
                "value_east": vb,
                "abs_diff": abs_diff,
                "rel_diff": round(rel_diff, 6),
                "diff_level": self._classify_level(rel_diff),
                "note": ""
            })

        # 按绝对差异降序
        records.sort(key=lambda r: abs(r["abs_diff"] or 0), reverse=True)

        return {
            "records": records,
            "diff_count": len(records),
            "match_count": match_count
        }

    def _classify_level(self, rel_diff: float) -> str:
        """差异等级判定（设计文档 §3.3 阈值规则）"""
        if rel_diff > self.THRESHOLDS["critical"]:
            return "critical"
        if rel_diff > self.THRESHOLDS["high"]:
            return "high"
        if rel_diff > self.THRESHOLDS["medium"]:
            return "medium"
        return "low"

    # ============================================
    # 归因说明
    # ============================================
    def _build_attribution(self, total_a: float, total_b: float,
                           abs_diff: float, rel_diff: float,
                           diffs: Dict[str, Any]) -> Dict[str, Any]:
        """生成差异归因：方向、量级、制度依据、建议"""
        direction = "EAST > 1104" if abs_diff > 0 else ("EAST < 1104" if abs_diff < 0 else "两口径一致")

        # 差异完全由资本化利息解释（EAST 含、1104 不含）
        reasons = []
        if abs_diff > 0:
            reasons.append(
                f"EAST 口径余额包含利息调整部分（资本化利息），1104 G01 口径仅统计纯本金，"
                f"差额 {abs_diff:,.2f} 元恰好等于样本内资本化利息总额"
            )
            reasons.append(
                "差异方向固定为 EAST ≥ 1104，属于制度定义差异而非数据质量问题"
            )
        critical_cnt = sum(1 for r in diffs["records"] if r["diff_level"] == "critical")
        if critical_cnt:
            reasons.append(f"其中 {critical_cnt} 笔相对差异超过 5%（critical 级），集中在资本化利息占比较高的贷款")

        conclusion = ("差异源于制度口径定义（EAST 含利息调整，1104 不含），属预期内差异"
                      if abs_diff > 0 else "两口径结果一致")

        return {
            "direction": direction,
            "reasons": reasons,
            "regulation_basis": [
                "1104 G01：个人住房贷款余额为报告期末纯本金余额",
                "EAST：贷款余额为会计账面余额，含应收未收利息的资本化部分"
            ],
            "suggestion": ("两套报表各自口径正确，无需调整；"
                           "跨表对账时应将资本化利息作为固定调节项，"
                           "调节公式：EAST余额 = 1104余额 + 资本化利息"),
            "conclusion": conclusion
        }

    # ============================================
    # 场景2：新旧逻辑回归（制度版本升级影响量化）
    # ============================================
    async def run_regression(self, report_pack: Dict[str, Any],
                             sql_old: str, sql_new: str) -> Dict[str, Any]:
        """在演示数据集上分别执行新旧两版转换 SQL，量化口径差异。

        安全路径：
          1) 两条 SQL 先过只读护栏（AST 白名单，仅 SELECT）；
          2) 结果物化到 twin_ 前缀临时表（演示库唯一被允许写/删的表族），
             之后统一走只读通道（database_mcp）取数比对；
          3) try/finally 兜底删除临时表，不留残留。

        约定：SQL 结果需包含逐笔键列（contract_no，缺省取第一列）与一个数值列
        （优先 balance/loan_balance/amount 命名，缺省取第一个数值列）。

        返回: {old_total, new_total, diff_amount, diff_rate, top_diffs,
               level_distribution, conclusion}
        """
        from backend.mcp.database_mcp import DatabaseMCPService
        from backend.utils.sql_guard import validate_readonly_sql

        if not sql_old or not sql_new:
            raise ValueError("sql_old / sql_new 均不能为空")
        # 第一层：只读护栏（两条都必须通过才动手）
        validate_readonly_sql(sql_old)
        validate_readonly_sql(sql_new)

        await demo_dataset.aensure_seeded()
        dbmcp = DatabaseMCPService({})

        old_table = "twin_regression_old"
        new_table = "twin_regression_new"
        try:
            # 物化到临时表（twin_ 前缀，drop 有白名单保护）
            await demo_dataset.aexecute_script([
                f"CREATE TABLE {old_table} AS {sql_old}",
                f"CREATE TABLE {new_table} AS {sql_new}",
            ])
            # 统一走只读通道取数
            res_old = await dbmcp.execute_sql(f"SELECT * FROM {old_table}")
            res_new = await dbmcp.execute_sql(f"SELECT * FROM {new_table}")
        finally:
            # 兜底清理，失败不影响主流程
            for t in (old_table, new_table):
                try:
                    await demo_dataset.adrop_table(t)
                except Exception:
                    pass

        rows_old = self._normalize_rows(res_old["rows"])
        rows_new = self._normalize_rows(res_new["rows"])

        # 复用差异分析引擎（按 contract_no 键逐笔比对 + 等级判定）
        diffs = self._analyze_diff(rows_old, rows_new)

        old_total = round(sum(r["balance"] for r in rows_old), 4)
        new_total = round(sum(r["balance"] for r in rows_new), 4)
        diff_amount = round(new_total - old_total, 4)
        diff_rate = diff_amount / old_total if old_total else 0

        level_dist: Dict[str, int] = {}
        for d in diffs["records"]:
            level_dist[d["diff_level"]] = level_dist.get(d["diff_level"], 0) + 1

        pack_name = (report_pack or {}).get("report_name", "未指定场景包")
        direction = "新口径 > 旧口径" if diff_amount > 0 else (
            "新口径 < 旧口径" if diff_amount < 0 else "两版逻辑结果一致")
        conclusion = (
            f"场景包[{pack_name}] 新旧逻辑回归：旧版总额 {old_total:,.2f}，"
            f"新版总额 {new_total:,.2f}，差异 {diff_amount:,.2f}（{diff_rate:.4%}），"
            f"{direction}；{diffs['diff_count']} 笔存在差异"
            f"（critical {level_dist.get('critical', 0)} / high {level_dist.get('high', 0)}）"
        )

        return {
            "report_pack_id": (report_pack or {}).get("id"),
            "old_total": old_total,
            "new_total": new_total,
            "diff_amount": diff_amount,
            "diff_rate": round(diff_rate, 6),
            "old_record_count": len(rows_old),
            "new_record_count": len(rows_new),
            "level_distribution": level_dist,
            "top_diffs": diffs["records"][:5],
            "conclusion": conclusion,
        }

    @staticmethod
    def _normalize_rows(rows: List[dict]) -> List[dict]:
        """把任意 SQL 结果规范化为差异引擎所需的 {contract_no, balance} 行。

        键列：优先 contract_no，否则第一列；数值列：优先 balance/loan_balance/amount
        命名，否则第一个数值类型列。找不到数值列时抛错（调用方应检查 SQL 形态）。
        """
        if not rows:
            return []
        cols = list(rows[0].keys())
        key_col = "contract_no" if "contract_no" in cols else cols[0]
        val_col = next((c for c in ("balance", "loan_balance", "amount") if c in cols), None)
        if val_col is None:
            for c in cols:
                if c == key_col:
                    continue
                if any(isinstance(r.get(c), (int, float)) for r in rows):
                    val_col = c
                    break
        if val_col is None:
            raise ValueError(f"SQL 结果缺少数值列（列：{cols}），无法做差异量化")
        return [{"contract_no": str(r.get(key_col)),
                 "balance": round(float(r.get(val_col) or 0), 4)} for r in rows]
