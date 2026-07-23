"""
Agent 1: 制度解析Agent (Regulation Parser)
职责: 检索制度，提取口径，识别陷阱

场景包驱动：任务关联 report_pack 时，检索关键词/报表类型/陷阱提示/映射建议
均从包定义读取（替换硬编码）；包缺失时回退原有行为（缺省 G01，兼容存量任务）。
"""

import time
from backend.agents.base import BaseAgent, AgentResult
from backend.services import report_pack_service


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
            # 0. 解析场景包（未指定时缺省 G01；包加载失败回退硬编码行为）
            pack = await report_pack_service.get_pack_safe(
                task_context.get("report_pack_id"))
            if pack:
                # 包定义回填任务上下文（任务显式指定的字段优先）
                task_context.setdefault("report_pack_id", pack["id"])
                if not task_context.get("report_type"):
                    task_context["report_type"] = pack["report_type"]
                if not task_context.get("target_table"):
                    task_context["target_table"] = pack["target_table"]
                if not task_context.get("source_tables"):
                    task_context["source_tables"] = list(pack["source_tables"])

            # 1. 解析用户意图
            report_type = task_context.get("report_type", "")
            report_code = task_context.get("report_code", "")
            section = task_context.get("section", "")

            # 2. 检索制度（场景包提供检索关键词时优先使用）
            if pack and pack.get("regulation_keywords"):
                query = f"{pack['regulation_keywords']} {section}".strip()
            else:
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

            # 场景包陷阱关键词：检索未命中时按包定义补充提示级陷阱
            if pack:
                traps = self._merge_pack_traps(traps, pack.get("trap_refs", []))

            # 4. 构建字段映射建议（场景包目标结构驱动）
            mapping_suggestions = self._suggest_mappings(rag_results, pack)

            duration_ms = int((time.time() - start_time) * 1000)

            return AgentResult(
                agent_name=self.name,
                status="success",
                output={
                    "regulation_summary": summary,
                    "traps_identified": traps,
                    "mapping_suggestions": mapping_suggestions,
                    "source_files": [r.get("source_file", "") for r in rag_results.get("results", [])],
                    "retrieved_count": rag_results.get("total_found", 0),
                    "report_pack_id": pack["id"] if pack else None,
                    "retrieval_query": query
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

    def _merge_pack_traps(self, traps: list, trap_refs: list) -> list:
        """合并场景包陷阱关键词：已在检索结果中出现的去重，未命中的补提示级"""
        merged = list(traps)
        existing_text = "".join(t.get("description", "") for t in traps)
        for keyword in trap_refs:
            if keyword and keyword not in existing_text:
                merged.append({
                    "level": "hint",
                    "description": f"【场景包陷阱提示】注意「{keyword}」相关口径处理"
                })
        return merged

    def _suggest_mappings(self, rag_results: dict, pack: dict = None) -> list:
        """建议字段映射
        场景包驱动：按包目标结构给出每个目标字段的口径文本与候选源表，
        供映射推断引擎（方案B）做五通道打分；无包时保持原有空列表行为。"""
        if not pack:
            return []
        return [
            {
                "target_field": f.get("field", ""),
                "caliber_text": f.get("caliber_text", ""),
                "data_type": f.get("data_type", ""),
                "required": f.get("required", False),
                "candidate_source_tables": list(pack.get("source_tables", [])),
            }
            for f in pack.get("target_schema", [])
        ]
