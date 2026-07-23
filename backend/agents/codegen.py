"""
Agent 2: 代码生成Agent (CodeGen)
职责: 生成SQL/Java转换代码，带注释，性能优化

场景包驱动：目标表/候选源表/目标结构/勾稽规则从 report_pack 读取（替换硬编码）；
任务显式指定的字段优先，包缺失时回退原有行为（缺省 G01，兼容存量任务）。
"""

import time
from backend.agents.base import BaseAgent, AgentResult
from backend.services import report_pack_service


class CodeGenAgent(BaseAgent):
    """代码生成Agent"""

    SYSTEM_PROMPT = """你是一位银行监管报送系统的资深ETL开发工程师。
请根据提供的制度口径和数据库Schema，生成完整的、可直接执行的转换SQL。

生成要求:
1. 必须是完整的 INSERT INTO ... SELECT ... 语句
2. 每个SELECT字段必须带行尾注释，说明转换逻辑
3. 金额字段使用 ROUND(..., 4) 精确计算
4. 日期字段使用标准函数
5. WHERE条件必须包含: is_deleted=0, is_test=0, org_no权限过滤
6. 无SELECT *，显式列出所有字段

输出格式:
```sql
-- 目标表: {target_table}
-- 源表: {source_tables}
-- 生成时间: {timestamp}
INSERT INTO {target_table} (...) SELECT ...
```

同时输出字段映射对照表:
| 目标字段 | 源字段 | 转换逻辑 | 可空 | 风险等级 |
"""

    def __init__(self):
        super().__init__(
            name="codegen",
            description="根据制度口径和Schema生成转换SQL/Java代码"
        )

    async def execute(self, task_context: dict, regulation_output: dict, **kwargs) -> AgentResult:
        start_time = time.time()

        try:
            # 0. 解析场景包（未指定时缺省 G01；包加载失败回退硬编码行为）
            pack = await report_pack_service.get_pack_safe(
                task_context.get("report_pack_id"))
            if pack:
                # 包定义回填任务上下文（任务显式指定的字段优先，下游 Agent 共享）
                task_context.setdefault("report_pack_id", pack["id"])
                if not task_context.get("source_tables"):
                    task_context["source_tables"] = list(pack["source_tables"])
                if not task_context.get("target_table"):
                    task_context["target_table"] = pack["target_table"]
                if not task_context.get("report_type"):
                    task_context["report_type"] = pack["report_type"]

            # 1. 获取源表Schema
            source_tables = task_context.get("source_tables", [])
            schemas = {}
            for table in source_tables:
                schema = await self._call_mcp(
                    "database_mcp.query_schema",
                    table_name=table
                )
                schemas[table] = schema

            # 2. 构建Prompt（含 HITL 专家确认映射注入段）
            user_prompt = self._build_prompt(task_context, regulation_output, schemas, pack)
            user_prompt += self._build_confirmed_mappings_section(kwargs.get("confirmed_mappings") or [])
            messages = [
                {"role": "system", "content": self.SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt}
            ]

            # 3. 调用AI生成代码
            response = await self._call_ai(messages)

            # 4. 解析输出
            content = response["choices"][0]["message"]["content"]
            code = self._extract_code(content)
            mapping_table = self._extract_mapping(content)

            duration_ms = int((time.time() - start_time) * 1000)

            return AgentResult(
                agent_name=self.name,
                status="success",
                output={
                    "generated_code": code,
                    "field_mapping": mapping_table,
                    "code_language": task_context.get("output_mode", "sql"),
                    "source_schemas": schemas,
                    "report_pack_id": pack["id"] if pack else None
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

    def _build_prompt(self, task_context: dict, regulation_output: dict,
                      schemas: dict, pack: dict = None) -> str:
        """构建代码生成Prompt（场景包提供目标结构/勾稽规则/陷阱关键词时注入）"""
        prompt = f"""请生成以下报送任务的转换SQL:

报送类型: {task_context.get("report_type", "")}
报表代码: {task_context.get("report_code", "")}
目标表: {task_context.get("target_table", "")}
源表: {", ".join(task_context.get("source_tables", []))}
输出模式: {task_context.get("output_mode", "sql")}
数据库方言: {task_context.get("dialect", "mysql")}

制度口径摘要:
{regulation_output.get("regulation_summary", {}).get("definition", "")}

关键约束:
"""
        for constraint in regulation_output.get("regulation_summary", {}).get("key_constraints", []):
            prompt += f"- {constraint}\n"

        prompt += "\n常见陷阱:\n"
        for trap in regulation_output.get("traps_identified", []):
            prompt += f"- [{trap.get('level', '')}] {trap.get('description', '')}\n"

        # 场景包：目标表结构（含逐字段口径）注入，替换原先完全依赖 Schema 推断
        if pack and pack.get("target_schema"):
            prompt += f"\n目标表结构（场景包 {pack['id']}，逐字段口径）:\n"
            for field in pack["target_schema"]:
                line = f"- {field.get('field', '')}: {field.get('data_type', '')}"
                if field.get("required"):
                    line += " [必填]"
                if field.get("caliber_text"):
                    line += f" 口径: {field['caliber_text']}"
                if field.get("expected_domain"):
                    line += f" 值域: {field['expected_domain']}"
                prompt += line + "\n"

        # 场景包：勾稽规则注入（生成 SQL 必须满足的核对口径）
        if pack and pack.get("reconciliation_rules"):
            prompt += "\n勾稽规则（生成结果必须满足）:\n"
            for rule in pack["reconciliation_rules"]:
                prompt += (f"- {rule.get('name', '')}: {rule.get('expression', '')}"
                           f"（容差 {rule.get('tolerance', 0)}）\n")

        prompt += "\n数据库Schema:\n"
        for table, schema in schemas.items():
            prompt += f"\n表 {table}:\n"
            for col in schema.get("columns", []):
                prompt += f"- {col['column_name']}: {col['data_type']}"
                if col.get("column_comment"):
                    prompt += f" ({col['column_comment']})"
                prompt += "\n"

        return prompt

    @staticmethod
    def _build_confirmed_mappings_section(confirmed_mappings: list) -> str:
        """HITL 专家确认映射注入段（范围C追加；无确认映射时返回空串，不影响原 prompt）

        confirmed_mappings: [{target_field, source_table, source_field, transform_rule, status}]
        """
        if not confirmed_mappings:
            return ""
        lines = ["", "以下映射已由专家确认，必须采用："]
        for m in confirmed_mappings:
            src = f"{m.get('source_table') or ''}.{m.get('source_field') or ''}".strip(".") or "（待ETL加工）"
            rule = m.get("transform_rule") or "DIRECT"
            lines.append(f"- {m.get('target_field', '')} <= {src} | 转换规则: {rule}")
        lines.append("生成SQL时上述字段映射不得更改，其余字段按制度口径推断。")
        return "\n".join(lines) + "\n"

    def _extract_code(self, content: str) -> str:
        """从AI响应中提取代码"""
        # 简单提取：找```sql和```之间的内容
        import re
        match = re.search(r"```sql\n(.*?)\n```", content, re.DOTALL)
        if match:
            return match.group(1).strip()
        return content

    def _extract_mapping(self, content: str) -> list:
        """从AI响应中提取字段映射表"""
        # 简化版：返回空列表，由前端解析
        return []
