"""
Agent 1: 制度解析Agent (Regulation Parser)
职责: 检索制度，提取口径，识别陷阱
"""

import time
from backend.agents.base import BaseAgent, AgentResult


class RegulationParserAgent(BaseAgent):
    """制度解析Agent"""

    def __init__(self):
        super().__init__(
            name="regulation_parser",
            description="检索监管制度，提取口径定义，识别常见陷阱"
        )

    async def execute(self, task_context: dict, **kwargs) -> AgentResult:
        start_time = time.time()

        try:
            # 1. 解析用户意图
            report_type = task_context.get("report_type", "")
            report_code = task_context.get("report_code", "")
            section = task_context.get("section", "")

            # 2. 检索制度
            query = f"{report_type} {report_code} {section} 口径"
            rag_results = await self._call_mcp(
                "regulation_rag.retrieve",
                query=query,
                doc_type=report_type,
                top_k=5
            )

            # 3. 提取口径摘要
            summary = self._extract_summary(rag_results)
            traps = self._identify_traps(rag_results)

            # 4. 构建字段映射建议
            mapping_suggestions = self._suggest_mappings(rag_results)

            duration_ms = int((time.time() - start_time) * 1000)

            return AgentResult(
                agent_name=self.name,
                status="success",
                output={
                    "regulation_summary": summary,
                    "traps_identified": traps,
                    "mapping_suggestions": mapping_suggestions,
                    "source_files": [r.get("source_file", "") for r in rag_results.get("results", [])],
                    "retrieved_count": rag_results.get("total_found", 0)
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

    def _extract_summary(self, rag_results: dict) -> dict:
        """提取口径摘要"""
        results = rag_results.get("results", [])
        summary = {
            "definition": "",
            "key_constraints": [],
            "common_traps": []
        }

        for result in results:
            content = result.get("content", "")
            # 简单提取：找"口径"、"定义"等关键词后的内容
            if "口径" in content or "定义" in content:
                summary["definition"] += content + "\n"
            if "约束" in content or "必须" in content:
                summary["key_constraints"].append(content)
            if "陷阱" in content or "注意" in content:
                summary["common_traps"].append(content)

        return summary

    def _identify_traps(self, rag_results: dict) -> list:
        """识别常见陷阱"""
        traps = []
        results = rag_results.get("results", [])

        for result in results:
            content = result.get("content", "")
            # 匹配【严重】【中等】【提示】标记
            if "【严重】" in content:
                traps.append({"level": "critical", "description": content})
            elif "【中等】" in content:
                traps.append({"level": "medium", "description": content})
            elif "【提示】" in content:
                traps.append({"level": "hint", "description": content})

        return traps

    def _suggest_mappings(self, rag_results: dict) -> list:
        """建议字段映射"""
        # 基于检索结果，建议源表字段到目标字段的映射
        suggestions = []
        # 简化版：返回空列表，由代码生成Agent根据Schema决定
        return suggestions
