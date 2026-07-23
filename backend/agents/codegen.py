"""
Agent 2: 代码生成Agent (CodeGen)
职责: 生成SQL/Java转换代码，带注释，性能优化
"""

import time
from backend.agents.base import BaseAgent, AgentResult


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
            # 1. 获取源表Schema
            source_tables = task_context.get("source_tables", [])
            schemas = {}
            for table in source_tables:
                schema = await self._call_mcp(
                    "database_mcp.query_schema",
                    table_name=table
                )
                schemas[table] = schema

            # 2. 构建Prompt
            messages = [
                {"role": "system", "content": self.SYSTEM_PROMPT},
                {"role": "user", "content": self._build_prompt(task_context, regulation_output, schemas)}
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
                    "source_schemas": schemas
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

    def _build_prompt(self, task_context: dict, regulation_output: dict, schemas: dict) -> str:
        """构建代码生成Prompt"""
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

        prompt += "\n数据库Schema:\n"
        for table, schema in schemas.items():
            prompt += f"\n表 {table}:\n"
            for col in schema.get("columns", []):
                prompt += f"- {col['column_name']}: {col['data_type']}"
                if col.get("column_comment"):
                    prompt += f" ({col['column_comment']})"
                prompt += "\n"

        return prompt

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
