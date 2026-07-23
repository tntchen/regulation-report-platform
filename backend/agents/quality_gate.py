"""
Agent 3: 质量校验Agent (Quality Gate)
职责: 对 Agent 2 生成的转换代码做六维质量校验，输出结构化校验报告与质量门禁判定

六维校验:
  1. 口径合规   —— 是否落实制度口径与 Agent 1 识别的陷阱（余额含利息调整、逾期90天分界、组合贷纳入等）
  2. 类型安全   —— 金额/利率精度处理（ROUND、DECIMAL 位数）是否匹配目标口径
  3. 空值防御   —— 可空字段是否有 IFNULL/COALESCE/CASE 等防御，避免 NULL 传播
  4. 性能友好   —— 无 SELECT *、过滤条件使用索引字段、避免对索引列套函数
  5. 安全合规   —— 银行安全红线：is_deleted/is_test 剔除、org_no 权限过滤、无危险 SQL、C2/C3 字段脱敏
  6. 监管特殊   —— 监管报送特有规则：利率 D20.6、逾期90天分界、公积金组合贷、时点余额口径

门禁判定:
  blocker —— 存在任一 blocker 级问题，阻断并回退 Agent 2 重试
  warn    —— 无 blocker 但有 warning，放行并记录
  pass    —— 全部通过，放行
"""

import re
import time
from typing import Dict, Any, List
from backend.agents.base import BaseAgent, AgentResult


class QualityGateAgent(BaseAgent):
    """质量校验Agent"""

    # 危险SQL关键字（银行安全红线，生成代码中绝不允许出现）
    DANGEROUS_KEYWORDS = ["DROP", "TRUNCATE", "ALTER", "GRANT", "DELETE FROM", "INTO OUTFILE", "LOAD_FILE"]

    # C2/C3 敏感字段（通用安全合规制度：日志与输出必须脱敏）
    SENSITIVE_COLUMNS = ["id_card", "phone", "cust_name"]

    def __init__(self):
        super().__init__(
            name="quality_gate",
            description="六维质量校验：口径合规/类型安全/空值防御/性能友好/安全合规/监管特殊"
        )

    async def execute(self, task_context: dict, codegen_output: dict = None,
                      regulation_output: dict = None, **kwargs) -> AgentResult:
        start_time = time.time()

        try:
            codegen_output = codegen_output or {}
            regulation_output = regulation_output or {}

            sql = codegen_output.get("generated_code", "")
            source_schemas = codegen_output.get("source_schemas", {})
            traps = regulation_output.get("traps_identified", [])

            # Agent 2 失败时直接阻断
            if not sql:
                report = self._build_blocked_report("Agent 2 未产出可校验的代码")
                return AgentResult(
                    agent_name=self.name,
                    status="success",
                    output=report,
                    duration_ms=int((time.time() - start_time) * 1000)
                )

            # 六维校验
            dimensions = {
                "caliber_compliance": self._check_caliber(sql, traps, task_context),
                "type_safety": self._check_type_safety(sql, source_schemas),
                "null_defense": self._check_null_defense(sql, source_schemas),
                "performance": self._check_performance(sql, source_schemas),
                "security_compliance": self._check_security(sql),
                "regulatory_special": self._check_regulatory(sql, task_context),
            }

            # 汇总门禁判定
            all_issues = []
            blocker_count = 0
            warning_count = 0
            for dim in dimensions.values():
                all_issues.extend(dim["issues"])
                blocker_count += sum(1 for i in dim["issues"] if i["level"] == "blocker")
                warning_count += sum(1 for i in dim["issues"] if i["level"] == "warning")

            if blocker_count > 0:
                gate_result = "block"
            elif warning_count > 0:
                gate_result = "warn"
            else:
                gate_result = "pass"

            # 生成自动修正建议（供 Agent 2 回退重试时参考）
            fix_suggestions = [
                f"[{issue['dimension']}] {issue['suggestion']}"
                for issue in all_issues
                if issue.get("suggestion")
            ]

            duration_ms = int((time.time() - start_time) * 1000)

            return AgentResult(
                agent_name=self.name,
                status="success",
                output={
                    "gate_result": gate_result,
                    "blocker_count": blocker_count,
                    "warning_count": warning_count,
                    "dimensions": dimensions,
                    "issues": all_issues,
                    "auto_fix_suggestions": fix_suggestions,
                    "summary": self._build_summary(gate_result, blocker_count, warning_count)
                },
                duration_ms=duration_ms
            )

        except Exception as e:
            return AgentResult(
                agent_name=self.name,
                status="failed",
                output={"gate_result": "block", "auto_fix_suggestions": []},
                error=str(e),
                duration_ms=int((time.time() - start_time) * 1000)
            )

    # ============================================
    # 维度1: 口径合规
    # ============================================
    def _check_caliber(self, sql: str, traps: List[dict], task_context: dict) -> Dict[str, Any]:
        """口径合规：制度口径与陷阱是否落实到生成代码中"""
        issues = []
        sql_lower = sql.lower()

        # EAST 口径：贷款余额必须含利息调整部分（本金 + 资本化利息）
        if "principal_balance" in sql_lower and "loan_balance" in sql_lower:
            if "interest_capitalized" not in sql_lower:
                issues.append({
                    "dimension": "caliber_compliance",
                    "level": "blocker",
                    "message": "贷款余额仅取 principal_balance，未含资本化利息（interest_capitalized），违反EAST账面余额口径",
                    "suggestion": "loan_balance 应计算为 ROUND(principal_balance + IFNULL(interest_capitalized, 0), 4)"
                })

        # Agent 1 识别出的 critical 陷阱必须在 SQL 中有对应处理痕迹
        for trap in traps:
            if trap.get("level") != "critical":
                continue
            desc = trap.get("description", "")
            if "公积金组合贷" in desc and "P001-G" not in sql:
                issues.append({
                    "dimension": "caliber_compliance",
                    "level": "warning",
                    "message": "制度陷阱提示公积金组合贷(P001-G)需纳入统计，生成代码中未见该产品编码",
                    "suggestion": "产品过滤条件加入 'P001-G'"
                })
            if "逾期" in desc and "90天" in desc and "overdue_days" not in sql_lower:
                issues.append({
                    "dimension": "caliber_compliance",
                    "level": "warning",
                    "message": "制度提示逾期本金90天分界口径，生成代码未涉及逾期字段",
                    "suggestion": "确认本报表是否需要逾期口径；若需要，按90天分界分段计算"
                })

        return self._dimension_result(issues)

    # ============================================
    # 维度2: 类型安全
    # ============================================
    def _check_type_safety(self, sql: str, source_schemas: dict) -> Dict[str, Any]:
        """类型安全：金额/利率字段精度处理"""
        issues = []
        sql_lower = sql.lower()

        # 金额字段（DECIMAL）参与运算时必须 ROUND
        amount_cols = self._find_columns_by_type(source_schemas, "DECIMAL")
        for col in amount_cols:
            col_lower = col.lower()
            if col_lower in sql_lower and f"round(" not in sql_lower:
                issues.append({
                    "dimension": "type_safety",
                    "level": "warning",
                    "message": f"金额字段 {col} 参与输出但未见 ROUND 精度控制",
                    "suggestion": f"金额字段使用 ROUND({col}, 4) 精确计算"
                })
                break  # 同类问题只报一次

        # 利率字段：监管要求 D20.6，至少 ROUND 6 位
        # 提取所有 ROUND(...) 表达式（支持一层嵌套括号，如 ROUND(IFNULL(x,0),6)）
        round_exprs = re.findall(r"round\s*\((?:[^()]|\([^()]*\))*\)", sql_lower)
        rate_cols = [c for c in amount_cols if "rate" in c.lower()]
        for col in rate_cols:
            col_lower = col.lower()
            if col_lower not in sql_lower:
                continue
            ok = any(col_lower in expr and re.search(r",\s*6\s*\)$", expr) for expr in round_exprs)
            if not ok:
                issues.append({
                    "dimension": "type_safety",
                    "level": "blocker",
                    "message": f"利率字段 {col} 未按 D20.6（6位小数）精度处理",
                    "suggestion": f"利率字段使用 ROUND({col}, 6)"
                })

        return self._dimension_result(issues)

    # ============================================
    # 维度3: 空值防御
    # ============================================
    def _check_null_defense(self, sql: str, source_schemas: dict) -> Dict[str, Any]:
        """空值防御：可空字段参与运算/输出时是否有 NULL 防御"""
        issues = []
        sql_lower = sql.lower()

        nullable_cols = self._find_nullable_columns(source_schemas)
        # 只关心参与表达式（加减乘除/CASE）的可空字段
        used_nullable = []
        for col in nullable_cols:
            col_lower = col.lower()
            if re.search(r"[+\-*/]\s*" + re.escape(col_lower), sql_lower) or \
               re.search(re.escape(col_lower) + r"\s*[+\-*/]", sql_lower) or \
               re.search(r"when\s+" + re.escape(col_lower), sql_lower):
                used_nullable.append(col)

        has_defense = any(k in sql_lower for k in ["ifnull", "coalesce", "case when"])
        if used_nullable and not has_defense:
            issues.append({
                "dimension": "null_defense",
                "level": "blocker",
                "message": f"可空字段 {', '.join(used_nullable)} 参与运算但无 IFNULL/COALESCE 防御，NULL 会导致结果整体为 NULL",
                "suggestion": "对可空字段使用 IFNULL(字段, 0) 或 COALESCE 包装"
            })

        return self._dimension_result(issues)

    # ============================================
    # 维度4: 性能友好
    # ============================================
    def _check_performance(self, sql: str, source_schemas: dict) -> Dict[str, Any]:
        """性能友好：全表扫描、SELECT *、索引列套函数"""
        issues = []
        sql_lower = sql.lower()

        if re.search(r"select\s+\*", sql_lower):
            issues.append({
                "dimension": "performance",
                "level": "blocker",
                "message": "存在 SELECT *，大表全字段读取会拖垮ETL性能",
                "suggestion": "显式列出需要的字段"
            })

        # WHERE 中对索引列套函数会导致索引失效
        indexed_cols = self._find_indexed_columns(source_schemas)
        where_match = re.search(r"where\s+(.*?)(?:;|$)", sql_lower, re.DOTALL)
        if where_match:
            where_clause = where_match.group(1)
            for col in indexed_cols:
                if re.search(r"(date_format|year|month|substr|left|concat)\s*\(\s*\w*\.*\s*" + re.escape(col.lower()), where_clause.lower()):
                    issues.append({
                        "dimension": "performance",
                        "level": "warning",
                        "message": f"WHERE 条件对索引列 {col} 使用函数，索引失效",
                        "suggestion": "改为对常量侧做运算，保持索引列裸列比较"
                    })

        # 缺少 WHERE 的查询（非全量初始化的增量任务）
        if not re.search(r"\bwhere\b", sql_lower):
            issues.append({
                "dimension": "performance",
                "level": "warning",
                "message": "生成SQL无 WHERE 过滤条件，疑似全表扫描",
                "suggestion": "至少包含 biz_date / is_deleted / org_no 过滤"
            })

        return self._dimension_result(issues)

    # ============================================
    # 维度5: 安全合规（银行安全红线）
    # ============================================
    def _check_security(self, sql: str) -> Dict[str, Any]:
        """安全合规：数据剔除、权限过滤、危险SQL、敏感字段脱敏"""
        issues = []
        sql_upper = sql.upper()
        sql_lower = sql.lower()

        # 危险关键字 —— 红线，直接阻断
        for keyword in self.DANGEROUS_KEYWORDS:
            if re.search(r"\b" + re.escape(keyword) + r"\b", sql_upper):
                issues.append({
                    "dimension": "security_compliance",
                    "level": "blocker",
                    "message": f"生成代码包含危险SQL关键字: {keyword}",
                    "suggestion": "移除所有 DDL/DML 危险操作，报送ETL只允许 INSERT INTO ... SELECT"
                })

        # 逻辑删除与测试数据剔除 —— 监管报送底线
        if "is_deleted" not in sql_lower:
            issues.append({
                "dimension": "security_compliance",
                "level": "blocker",
                "message": "缺少 is_deleted=0 过滤，逻辑删除数据会进入报送结果",
                "suggestion": "WHERE 条件加入 is_deleted = 0"
            })
        if "is_test" not in sql_lower:
            issues.append({
                "dimension": "security_compliance",
                "level": "blocker",
                "message": "缺少 is_test=0 过滤，测试数据会污染报送结果",
                "suggestion": "WHERE 条件加入 is_test = 0"
            })

        # 机构权限过滤 —— 跨机构数据必须隔离
        if "org_no" not in sql_lower:
            issues.append({
                "dimension": "security_compliance",
                "level": "blocker",
                "message": "缺少 org_no 机构权限过滤，存在跨机构数据越权风险",
                "suggestion": "WHERE 条件加入 org_no 机构过滤"
            })

        # C2/C3 敏感字段明文输出 —— 通用安全合规制度要求脱敏
        for col in self.SENSITIVE_COLUMNS:
            if re.search(r"\b" + col + r"\b", sql_lower):
                masked = re.search(r"(concat|substr|left|mask)\s*\([^)]*" + col, sql_lower)
                if not masked:
                    issues.append({
                        "dimension": "security_compliance",
                        "level": "warning",
                        "message": f"敏感字段 {col}（C2级）疑似明文输出，违反信息分级脱敏要求",
                        "suggestion": f"{col} 脱敏输出：保留前3后4，中间以 * 代替"
                    })

        return self._dimension_result(issues)

    # ============================================
    # 维度6: 监管特殊
    # ============================================
    def _check_regulatory(self, sql: str, task_context: dict) -> Dict[str, Any]:
        """监管特殊规则：利率精度、逾期90天分界、组合贷、时点余额"""
        issues = []
        sql_lower = sql.lower()
        report_type = task_context.get("report_type", "")

        # 逾期口径：使用 overdue_days 时必须有 90 天分界逻辑
        if "overdue_days" in sql_lower:
            if not re.search(r">\s*=\s*91|>\s*90|>=\s*91", sql_lower):
                issues.append({
                    "dimension": "regulatory_special",
                    "level": "blocker",
                    "message": "使用 overdue_days 但未实现 90天分界规则（90天以内按已逾期部分，91天及以上按整笔本金）",
                    "suggestion": "CASE WHEN overdue_days >= 91 THEN 整笔本金 WHEN overdue_days > 0 THEN 已逾期部分 ELSE 0 END"
                })

        # 利率报备：LPR 与精度要求
        if "利率" in report_type or "rate_report" in task_context.get("report_code", "").lower():
            if "lpr" not in sql_lower:
                issues.append({
                    "dimension": "regulatory_special",
                    "level": "warning",
                    "message": "利率报备任务未见 LPR 基准相关计算",
                    "suggestion": "确认是否需要计算 执行利率 - LPR 浮动区间（BP）"
                })

        # 住房贷款口径：公积金组合贷必须纳入
        if "product_code" in sql_lower and "p001" in sql_lower.replace("'", "").replace('"', ""):
            normalized = sql_lower.replace("'", "").replace('"', "")
            if "p001" in normalized and "p001-g" not in normalized:
                issues.append({
                    "dimension": "regulatory_special",
                    "level": "warning",
                    "message": "按 P001 筛选住房贷款但未含 P001-G（公积金组合贷），会漏报组合贷余额",
                    "suggestion": "产品过滤改为 product_code IN ('P001', 'P001-G')"
                })

        return self._dimension_result(issues)

    # ============================================
    # 工具方法
    # ============================================
    def _dimension_result(self, issues: List[dict]) -> Dict[str, Any]:
        """汇总单维度结果：有 blocker 则 blocker，有 warning 则 warning，否则 pass"""
        if any(i["level"] == "blocker" for i in issues):
            status = "blocker"
        elif any(i["level"] == "warning" for i in issues):
            status = "warning"
        else:
            status = "pass"
        return {"status": status, "issues": issues}

    def _build_blocked_report(self, reason: str) -> Dict[str, Any]:
        """上游失败时的直接阻断报告"""
        return {
            "gate_result": "block",
            "blocker_count": 1,
            "warning_count": 0,
            "dimensions": {},
            "issues": [{
                "dimension": "pipeline",
                "level": "blocker",
                "message": reason,
                "suggestion": "检查 Agent 2 代码生成阶段的错误日志后重试"
            }],
            "auto_fix_suggestions": ["检查 Agent 2 代码生成阶段的错误日志后重试"],
            "summary": f"质量门禁阻断：{reason}"
        }

    def _build_summary(self, gate_result: str, blocker_count: int, warning_count: int) -> str:
        """生成门禁结论摘要"""
        if gate_result == "block":
            return f"质量门禁阻断：发现 {blocker_count} 个阻断级问题、{warning_count} 个警告，已回退代码生成Agent重试"
        if gate_result == "warn":
            return f"质量门禁放行（带警告）：{warning_count} 个警告已记录，建议人工复核"
        return "质量门禁通过：六维校验全部通过"

    def _find_columns_by_type(self, source_schemas: dict, type_keyword: str) -> List[str]:
        """从源表Schema中找出指定类型的字段名"""
        cols = []
        for schema in source_schemas.values():
            for col in schema.get("columns", []):
                if type_keyword.upper() in col.get("data_type", "").upper():
                    cols.append(col["column_name"])
        return cols

    def _find_nullable_columns(self, source_schemas: dict) -> List[str]:
        """从源表Schema中找出可空字段名"""
        cols = []
        for schema in source_schemas.values():
            for col in schema.get("columns", []):
                if col.get("is_nullable") == "YES":
                    cols.append(col["column_name"])
        return cols

    def _find_indexed_columns(self, source_schemas: dict) -> List[str]:
        """从源表Schema中找出索引字段名"""
        cols = []
        for schema in source_schemas.values():
            for col in schema.get("columns", []):
                if col.get("is_index") or col.get("is_pk"):
                    cols.append(col["column_name"])
        return cols
