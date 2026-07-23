"""
任务编排引擎
支持6Agent串行+并行调度，质量门禁，重试回退

执行链路:
  Agent1 制度解析 → Agent2 代码生成 → Agent3 质量校验（门禁①）
      → [Agent4 测试验证 ∥ Agent5 数字孪生]（并行，门禁②在 Agent4）
      → Agent6 投产交付

门禁规则:
  - Agent 3 block → 回退 Agent 2 重试（最多 MAX_GATE_RETRY 次）
  - Agent 4 关键项 fail → 回退 Agent 2 重试（同上）
  - 超过最大重试 → 任务 failed 并记录失败原因
  - warning 放行并记录

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

    async def execute_task(self, task_context: Dict[str, Any]) -> Dict[str, Any]:
        """执行任务"""
        start_time = time.time()

        # 任务状态
        state = {
            "task_id": task_context.get("task_id", ""),
            "tenant_id": self.tenant_id,
            "status": "executing",
            "current_stage": "regulation_parser",
            "progress": 0,
            "stages": [],
            "outputs": {},
            "retry_count": 0,
            "start_time": start_time
        }
        # 登记初始状态（供 API 查询）
        task_service.save_task_state(state)

        # 按DAG执行
        current_agents = ["regulation_parser"]
        completed_outputs = {}

        # 门禁回退标记：本层结束后强制跳转回 codegen
        rollback_to_codegen = False

        while current_agents:
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
                    task_service.save_task_state(state)
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

                # 门禁①：质量校验 block → 回退 codegen
                if agent_name == "quality_gate":
                    gate_result = result.output.get("gate_result", "pass")
                    if gate_result == "block":
                        rollback = self._handle_gate_block(
                            state, task_context,
                            result.output.get("auto_fix_suggestions", []),
                            start_time
                        )
                        if rollback == "failed":
                            return state
                        rollback_to_codegen = True

                # 门禁②：测试验证关键项 fail → 回退 codegen
                elif agent_name == "test_verify":
                    if result.output.get("critical_fail"):
                        rollback = self._handle_gate_block(
                            state, task_context,
                            result.output.get("fail_reasons", []),
                            start_time
                        )
                        if rollback == "failed":
                            return state
                        rollback_to_codegen = True

                completed_outputs[agent_name] = result.output

            # 每完成一层，刷新任务状态
            task_service.save_task_state(state)

            # 门禁阻断：回退到 codegen 重新生成
            if rollback_to_codegen:
                rollback_to_codegen = False
                completed_outputs.pop("quality_gate", None)
                completed_outputs.pop("test_verify", None)
                completed_outputs.pop("digital_twin", None)
                current_agents = ["codegen"]
                continue

            # 确定下一层Agent
            next_agents = set()
            for agent_name in current_agents:
                for next_agent in self.AGENT_DAG[agent_name]["next"]:
                    if self._check_prerequisites(next_agent, list(completed_outputs.keys())):
                        next_agents.add(next_agent)

            current_agents = list(next_agents)

        state["status"] = "completed"
        state["duration_ms"] = int((time.time() - start_time) * 1000)
        task_service.save_task_state(state)

        return state

    def _handle_gate_block(self, state: dict, task_context: dict,
                           suggestions: list, start_time: float) -> str:
        """处理门禁阻断：更新重试计数，超限则标记任务失败
        返回 "retry" 表示回退重试，"failed" 表示超限失败"""
        state["retry_count"] += 1
        if state["retry_count"] > self.MAX_GATE_RETRY:
            state["status"] = "failed"
            state["error"] = f"质量门禁多次阻断，超过最大重试次数({self.MAX_GATE_RETRY})"
            state["duration_ms"] = int((time.time() - start_time) * 1000)
            task_service.save_task_state(state)
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
