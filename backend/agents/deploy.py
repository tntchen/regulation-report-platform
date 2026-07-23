"""
Agent 6: 投产交付Agent (Deploy)
职责: 汇总前序全部 Agent 产出，生成交付物并写入任务工作目录 data/tasks/{task_id}/。

交付物:
  01_转换逻辑说明.md   —— 任务信息、生成SQL、转换逻辑说明
  02_口径映射表.md     —— 字段映射 + 制度口径依据 + 识别陷阱
  03_校验结论摘要.md   —— 质量门禁六维结果 + 测试验证 7 项结果
  04_投产Checklist.md  —— 投产前检查清单（含数字孪生差异结论）
"""

import os
import time
from datetime import datetime
from typing import Dict, Any, List
from backend.agents.base import BaseAgent, AgentResult
from backend.config import settings


class DeployAgent(BaseAgent):
    """投产交付Agent"""

    def __init__(self):
        super().__init__(
            name="deploy",
            description="汇总全链路产出，生成投产交付物（Markdown 文档集）"
        )

    async def execute(self, task_context: dict, all_outputs: dict = None, **kwargs) -> AgentResult:
        start_time = time.time()

        try:
            all_outputs = all_outputs or {}
            task_id = task_context.get("task_id", "UNKNOWN")

            # 任务工作目录
            work_dir = os.path.join(settings.task_work_dir, task_id)
            os.makedirs(work_dir, exist_ok=True)

            regulation = all_outputs.get("regulation_parser", {})
            codegen = all_outputs.get("codegen", {})
            quality = all_outputs.get("quality_gate", {})
            test = all_outputs.get("test_verify", {})
            twin = all_outputs.get("digital_twin", {})

            # 生成四份交付物
            deliverables = []
            deliverables.append(self._write(work_dir, "01_转换逻辑说明.md",
                                            self._render_transform_doc(task_context, codegen)))
            deliverables.append(self._write(work_dir, "02_口径映射表.md",
                                            self._render_mapping_doc(task_context, regulation)))
            deliverables.append(self._write(work_dir, "03_校验结论摘要.md",
                                            self._render_quality_doc(quality, test)))
            deliverables.append(self._write(work_dir, "04_投产Checklist.md",
                                            self._render_checklist(task_context, quality, test, twin)))

            duration_ms = int((time.time() - start_time) * 1000)

            return AgentResult(
                agent_name=self.name,
                status="success",
                output={
                    "work_dir": work_dir,
                    "deliverables": deliverables,
                    "deliverable_count": len(deliverables),
                    "summary": f"已生成 {len(deliverables)} 份投产交付物至 {work_dir}"
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
    # 交付物渲染
    # ============================================
    def _write(self, work_dir: str, filename: str, content: str) -> Dict[str, Any]:
        """写入交付物文件，返回清单项"""
        path = os.path.join(work_dir, filename)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return {
            "filename": filename,
            "path": path,
            "size": len(content.encode("utf-8"))
        }

    def _header(self, title: str, task_context: dict) -> str:
        return (f"# {title}\n\n"
                f"- 任务编号: {task_context.get('task_id', '')}\n"
                f"- 租户: {task_context.get('tenant_id', '')}\n"
                f"- 报送类型: {task_context.get('report_type', '')} / {task_context.get('report_code', '')}\n"
                f"- 生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n---\n\n")

    def _render_transform_doc(self, task_context: dict, codegen: dict) -> str:
        """01 转换逻辑说明"""
        doc = self._header("转换逻辑说明文档", task_context)
        doc += "## 1. 任务概述\n\n"
        doc += f"- 源表: {', '.join(task_context.get('source_tables', []))}\n"
        doc += f"- 目标表: {task_context.get('target_table', '')}\n"
        doc += f"- 输出模式: {task_context.get('output_mode', 'sql')}（方言: {task_context.get('dialect', 'mysql')}）\n\n"
        doc += "## 2. 生成的转换 SQL\n\n"
        doc += "```sql\n" + codegen.get("generated_code", "（无）") + "\n```\n\n"
        doc += "## 3. 转换逻辑要点\n\n"
        doc += "- 贷款余额 = 本金余额 + 资本化利息（EAST 账面余额口径），ROUND 4 位\n"
        doc += "- 利率按 D20.6 处理，ROUND 6 位\n"
        doc += "- 逾期本金按 90 天分界：91 天及以上整笔本金，90 天以内已逾期部分\n"
        doc += "- 数据过滤：is_deleted=0、is_test=0、org_no 机构权限过滤\n"
        return doc

    def _render_mapping_doc(self, task_context: dict, regulation: dict) -> str:
        """02 口径映射表"""
        doc = self._header("口径映射表", task_context)
        doc += "## 1. 制度口径依据\n\n"
        sources = regulation.get("source_files", [])
        if sources:
            for s in sources:
                doc += f"- {s}\n"
        else:
            doc += "- （内置演示口径）\n"
        doc += "\n## 2. 识别的关键陷阱\n\n"
        doc += "| 级别 | 陷阱描述 |\n| --- | --- |\n"
        traps = regulation.get("traps_identified", [])
        if traps:
            for t in traps:
                desc = t.get("description", "").replace("\n", " ")[:80]
                doc += f"| {t.get('level', '')} | {desc} |\n"
        else:
            doc += "| - | 本次未识别到陷阱（可能制度检索未命中） |\n"
        doc += "\n## 3. 字段映射对照\n\n"
        doc += "| 目标字段 | 源字段 | 转换逻辑 | 口径依据 |\n| --- | --- | --- | --- |\n"
        doc += "| loan_balance | principal_balance + interest_capitalized | 本金+资本化利息，ROUND 4 位 | EAST 账面余额 |\n"
        doc += "| execute_rate | execute_rate | ROUND 6 位 | 利率 D20.6 |\n"
        doc += "| overdue_principal | overdue_days, principal_balance | 90 天分界分段 | EAST 逾期口径 |\n"
        return doc

    def _render_quality_doc(self, quality: dict, test: dict) -> str:
        """03 校验结论摘要"""
        doc = "# 校验结论摘要\n\n"
        doc += f"- 生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n---\n\n"

        doc += "## 1. 质量门禁（Agent 3 六维校验）\n\n"
        doc += f"- 门禁判定: **{quality.get('gate_result', 'N/A')}**\n"
        doc += f"- blocker: {quality.get('blocker_count', 0)} / warning: {quality.get('warning_count', 0)}\n"
        doc += f"- 结论: {quality.get('summary', '')}\n\n"
        doc += "| 维度 | 结果 |\n| --- | --- |\n"
        for dim_name, dim in quality.get("dimensions", {}).items():
            doc += f"| {dim_name} | {dim.get('status', '')} |\n"

        doc += "\n## 2. 测试验证（Agent 4 七项校验）\n\n"
        doc += f"- 总体结果: **{test.get('overall_result', 'N/A')}**（{test.get('summary', '')}）\n\n"
        doc += "| 校验项 | 结果 | 说明 |\n| --- | --- | --- |\n"
        for c in test.get("checks", []):
            doc += f"| {c.get('name', '')} | {c.get('status', '')} | {c.get('detail', '')} |\n"
        return doc

    def _render_checklist(self, task_context: dict, quality: dict,
                          test: dict, twin: dict) -> str:
        """04 投产 Checklist"""
        doc = self._header("投产 Checklist", task_context)

        gate_ok = quality.get("gate_result") in ("pass", "warn")
        test_ok = test.get("overall_result") == "pass"

        doc += "## 1. 自动校验项\n\n"
        doc += f"- [{'x' if gate_ok else ' '}] 质量门禁通过（{quality.get('gate_result', 'N/A')}）\n"
        doc += f"- [{'x' if test_ok else ' '}] 测试验证通过（{test.get('overall_result', 'N/A')}）\n"

        twin_diff = twin.get("diff_analysis", {})
        if twin_diff:
            doc += (f"- [x] 数字孪生差异分析完成：两口径差异 "
                    f"{twin_diff.get('abs_diff_total', 0):,.2f} 元"
                    f"（{twin_diff.get('rel_diff_total', 0):.4%}），已归因\n")

        doc += "\n## 2. 人工确认项\n\n"
        doc += "- [ ] 业务负责人确认口径映射表与制度原文一致\n"
        doc += "- [ ] 确认目标表结构与下游报送系统字段定义匹配\n"
        doc += "- [ ] 确认机构权限过滤范围（org_no）与本次报送机构一致\n"
        doc += "- [ ] 敏感字段（C2/C3）输出已脱敏，日志无明文\n"
        doc += "- [ ] 调度窗口与报送截止时间匹配，回退方案已准备\n"

        doc += "\n## 3. 数字孪生差异结论\n\n"
        if twin:
            doc += f"- 场景: {twin.get('scenario', '')}\n"
            attr = twin.get("attribution", {})
            doc += f"- 归因: {attr.get('conclusion', '')}\n"
            doc += f"- 建议: {attr.get('suggestion', '')}\n"
        else:
            doc += "- （数字孪生未执行）\n"
        return doc
