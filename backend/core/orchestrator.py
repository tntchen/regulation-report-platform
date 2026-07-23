"""
任务编排引擎
支持6Agent串行+并行调度，质量门禁，重试回退

执行链路:
  Agent1 制度解析 → Agent2 代码生成 → Agent3 质量校验（门禁①）
      → [Agent4 测试验证 ∥ Agent5 数字孪生]（并行，门禁②在 Agent4）
      → Agent6 投产交付

门禁规则:
  - Agent 3 block → 回退重试（最多 MAX_GATE_RETRY 次）
  - Agent 4 关键项 fail → 回退重试（同上）
  - 回退从 regulation_parser 重跑：刷新检索上下文，避免基于过期口径重新生成
  - 超过最大重试 → 任务 failed 并记录失败原因
  - warning 放行并记录

L2-D4：任务由后台 worker 异步执行；每层完成即落库断点（checkpoint），
进程重启后从断点续跑；阶段边界检查取消标记，支持优雅取消。

任务状态通过 task_service 登记，供 API 实时查询阶段明细。
"""

import asyncio
import time
from typing import Dict, Any, List
from backend.agents.base import AgentResult
from backend.agents.regulation_parser import RegulationParserAgent
from backend.agents.codegen import CodeGenAgent
from backend.agents.quality_gate import QualityGateAgent
from backend.agents.test_verify import TestVerifyAgent
from backend.agents.digital_twin import DigitalTwinAgent
from backend.agents.deploy import DeployAgent
from backend.core.ai_adapter import AIAdapterFactory
from backend.mcp.database_mcp import DatabaseMCPService
from backend.mcp.regulation_rag import RegulationRAGService
from backend.services import task_service


class TaskOrchestrator:
    """任务编排引擎"""

    # Agent执行DAG定义
    AGENT_DAG = {
        "regulation_parser": {"next": ["codegen"], "parallel": False},
        "codegen": {"next": ["quality_gate"], "parallel": False},
        "quality_gate": {
            "next": ["test_verify", "digital_twin"],
            "parallel": True,
            "gate_check": True
        },
        "test_verify": {"next": ["deploy"], "parallel": False, "gate_check": True},
        "digital_twin": {"next": ["deploy"], "parallel": False},
        "deploy": {"next": [], "parallel": False}
    }

    # 质量门禁最大回退重试次数
    MAX_GATE_RETRY = 3

    def __init__(self, tenant_id: str):
        self.tenant_id = tenant_id
        self.ai_backend = AIAdapterFactory.get_adapter(tenant_id)

        # 初始化MCP服务
        tenant_config = self._get_tenant_config(tenant_id)
        self.mcp_services = {
            "database_mcp": DatabaseMCPService(tenant_config.get("data_sources", [{}])[0]),
            "regulation_rag": RegulationRAGService(tenant_id)
        }

        # 初始化Agent
        self.agents = self._init_agents()

    def _get_tenant_config(self, tenant_id: str) -> dict:
        """获取租户配置"""
        from backend.core.tenant_context import PRESET_TENANTS
        return PRESET_TENANTS.get(tenant_id, {})

    def _init_agents(self) -> Dict[str, Any]:
        """初始化所有Agent"""
        agents = {}

        def _wire(agent):
            agent.set_ai_backend(self.ai_backend)
            agent.set_mcp_tools(self.mcp_services)
            return agent

        # Agent 1-3: 制度解析 → 代码生成 → 质量校验
        agents["regulation_parser"] = _wire(RegulationParserAgent())
        agents["codegen"] = _wire(CodeGenAgent())
        agents["quality_gate"] = _wire(QualityGateAgent())

        # Agent 4-6: 测试验证 / 数字孪生 / 投产交付
        agents["test_verify"] = _wire(TestVerifyAgent())
        agents["digital_twin"] = _wire(DigitalTwinAgent())
        agents["deploy"] = _wire(DeployAgent())

        return agents

    async def execute_task(self, task_context: Dict[str, Any],
                           resume_state: Dict[str, Any] = None,
                           should_cancel=None) -> Dict[str, Any]:
        """执行任务

        resume_state: 断点恢复时传入持久化的任务状态（worker 重启续跑），
                      已完成阶段不重复执行，从 checkpoint["next"] 指定的阶段续跑。
        should_cancel: 可选异步回调，阶段边界检查取消标记（任务取消机制）。
        """
        start_time = time.time()

        if resume_state:
            # 断点恢复：复用已持久化的阶段与产出
            state = resume_state
            state["status"] = "executing"
            state["error"] = None
            completed_outputs = {k: v for k, v in (state.get("outputs") or {}).items() if v}
            current_agents = list((state.get("checkpoint") or {}).get("next") or [])
            if not current_agents:
                # 断点缺失/损坏：无法安全定位续跑点，明确判 failed
                state["status"] = "failed"
                state["error"] = "进程重启后断点信息缺失，无法安全续跑"
                state["duration_ms"] = int((time.time() - start_time) * 1000)
                await task_service.save_task_state(state)
                return state
        else:
            # 新任务：初始化状态
            state = {
                "task_id": task_context.get("task_id", ""),
                "tenant_id": self.tenant_id,
                "task_type": task_context.get("report_type", "report"),
                "name": f"{task_context.get('report_type', '')} {task_context.get('report_code', '')} 报送任务".strip(),
                "status": "executing",
                "current_stage": "regulation_parser",
                "progress": 0,
                "stages": [],
                "outputs": {},
                "report_config": task_context,
                "retry_count": 0,
                "start_time": start_time,
                "checkpoint": {"completed": [], "next": ["regulation_parser"]},
            }
            completed_outputs = {}
            current_agents = ["regulation_parser"]

        # 登记状态（供 API 查询）
        await task_service.save_task_state(state)

        async def _cancelled() -> bool:
            """阶段边界取消检查"""
            if should_cancel is None:
                return False
            return await should_cancel()

        # 门禁回退标记：本层结束后强制跳转回 regulation_parser 刷新检索上下文
        rollback_to_parser = False

        while current_agents:
            # 阶段边界：取消检查（优雅终止）
            if await _cancelled():
                state["status"] = "cancelled"
                state["error"] = "任务被用户取消"
                state["duration_ms"] = int((time.time() - start_time) * 1000)
                await task_service.save_task_state(state)
                return state

            # 执行当前层Agent（同一层多个Agent时并行执行，如 test_verify + digital_twin）
            layer_results = await asyncio.gather(*[
                self._execute_agent(agent_name, task_context, completed_outputs)
                for agent_name in current_agents
            ])

            for agent_name, result in zip(current_agents, layer_results):
                state["stages"].append(result.to_dict())
                state["outputs"][agent_name] = result.output
                state["current_stage"] = agent_name

                # Agent 执行失败直接终止
                if result.status == "failed":
                    state["status"] = "failed"
                    state["error"] = f"Agent {agent_name} 执行失败: {result.error}"
                    state["duration_ms"] = int((time.time() - start_time) * 1000)
                    await task_service.save_task_state(state)
                    return state

                # 更新进度
                progress_map = {
                    "regulation_parser": 15,
                    "codegen": 35,
                    "quality_gate": 50,
                    "test_verify": 70,
                    "digital_twin": 85,
                    "deploy": 100
                }
                state["progress"] = progress_map.get(agent_name, 0)

                # 门禁①：质量校验 block → 回退重试
                if agent_name == "quality_gate":
                    gate_result = result.output.get("gate_result", "pass")
                    if gate_result == "block":
                        rollback = await self._handle_gate_block(
                            state, task_context,
                            result.output.get("auto_fix_suggestions", []),
                            start_time
                        )
                        if rollback == "failed":
                            return state
                        rollback_to_parser = True

                # 门禁②：测试验证关键项 fail → 回退重试
                elif agent_name == "test_verify":
                    if result.output.get("critical_fail"):
                        rollback = await self._handle_gate_block(
                            state, task_context,
                            result.output.get("fail_reasons", []),
                            start_time
                        )
                        if rollback == "failed":
                            return state
                        rollback_to_parser = True

                completed_outputs[agent_name] = result.output

            # 每完成一层，刷新任务状态与断点
            self._update_checkpoint(state, current_agents, completed_outputs)
            await task_service.save_task_state(state)

            # 门禁阻断：回退到 regulation_parser 重跑（刷新检索上下文，避免过期口径）
            if rollback_to_parser:
                rollback_to_parser = False
                for stale in ("regulation_parser", "codegen", "quality_gate",
                              "test_verify", "digital_twin"):
                    completed_outputs.pop(stale, None)
                    state["outputs"].pop(stale, None)
                current_agents = ["regulation_parser"]
                # 回退中的断点：若进程在此死亡，重启后从 regulation_parser 续跑
                state["checkpoint"] = {"completed": [], "next": ["regulation_parser"]}
                await task_service.save_task_state(state)
                continue

            # 确定下一层Agent
            next_agents = set()
            for agent_name in current_agents:
                for next_agent in self.AGENT_DAG[agent_name]["next"]:
                    if self._check_prerequisites(next_agent, list(completed_outputs.keys())):
                        next_agents.add(next_agent)

            current_agents = list(next_agents)
            # 更新断点：下一阶段
            state["checkpoint"] = {
                "completed": list(completed_outputs.keys()),
                "next": list(current_agents),
            }
            await task_service.save_task_state(state)

        state["status"] = "completed"
        state["duration_ms"] = int((time.time() - start_time) * 1000)
        state["checkpoint"] = {"completed": list(completed_outputs.keys()), "next": []}
        await task_service.save_task_state(state)

        return state

    @staticmethod
    def _update_checkpoint(state: dict, finished_layer: list, completed_outputs: dict):
        """每层完成后更新断点（completed 为已完成 Agent 集合，next 由主循环随后精确计算）"""
        completed = set((state.get("checkpoint") or {}).get("completed") or [])
        completed.update(finished_layer)
        state["checkpoint"] = {
            "completed": sorted(completed),
            "next": (state.get("checkpoint") or {}).get("next", []),
        }

    async def _handle_gate_block(self, state: dict, task_context: dict,
                           suggestions: list, start_time: float) -> str:
        """处理门禁阻断：更新重试计数，超限则标记任务失败
        返回 "retry" 表示回退重试，"failed" 表示超限失败"""
        state["retry_count"] += 1
        if state["retry_count"] > self.MAX_GATE_RETRY:
            state["status"] = "failed"
            state["error"] = f"质量门禁多次阻断，超过最大重试次数({self.MAX_GATE_RETRY})"
            state["duration_ms"] = int((time.time() - start_time) * 1000)
            await task_service.save_task_state(state)
            return "failed"
        # 修正建议注入 task_context，供 codegen 重试时参考
        task_context["fix_suggestions"] = suggestions
        return "retry"

    async def _execute_agent(self, agent_name: str, task_context: dict, completed_outputs: dict) -> AgentResult:
        """执行单个Agent"""
        agent = self.agents.get(agent_name)
        if not agent:
            return AgentResult(agent_name=agent_name, status="failed", output={}, error="Agent未找到")

        # 准备参数
        kwargs = {"task_context": task_context}

        if agent_name == "codegen":
            kwargs["regulation_output"] = completed_outputs.get("regulation_parser", {})
            if "fix_suggestions" in task_context:
                kwargs["fix_suggestions"] = task_context["fix_suggestions"]

        elif agent_name == "quality_gate":
            kwargs["codegen_output"] = completed_outputs.get("codegen", {})
            kwargs["regulation_output"] = completed_outputs.get("regulation_parser", {})

        elif agent_name == "test_verify":
            kwargs["codegen_output"] = completed_outputs.get("codegen", {})
            kwargs["regulation_output"] = completed_outputs.get("regulation_parser", {})

        elif agent_name == "digital_twin":
            # 数字孪生仅依赖任务上下文与演示数据集，独立并行
            pass

        elif agent_name == "deploy":
            kwargs["all_outputs"] = completed_outputs

        return await agent.execute(**kwargs)

    def _check_prerequisites(self, agent_name: str, completed: List[str]) -> bool:
        """检查前置依赖是否完成"""
        # 检查所有前置Agent是否已完成
        for prev_agent, config in self.AGENT_DAG.items():
            if agent_name in config["next"]:
                if prev_agent not in completed:
                    return False
        return True
