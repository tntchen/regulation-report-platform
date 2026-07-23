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
import uuid
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
from backend.utils.logging import get_logger

logger = get_logger(__name__)

# 映射终态（人工已处理的映射状态，见 docs/映射工作台与场景包设计方案.md §1.2）
MAPPING_TERMINAL_STATUS = ("confirmed", "modified", "needs_etl")
# 映射推断高置信阈值：全部 ≥ 该值且任务 auto_mode 时可跳过人工确认
MAPPING_AUTO_THRESHOLD = 0.85


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

        # HITL 映射门禁开关：仅任务创建时显式指定 report_pack_id 才启用。
        # 必须在执行前快照——Agent 1 会 setdefault 回填缺省 G01 到 task_context，
        # 若执行中再读会把存量任务误判为显式指定（导致老任务被挂起，破坏演示路径）
        self._mapping_gate_enabled = bool(task_context.get("report_pack_id"))

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

            # HITL 映射确认门禁：regulation_parser 完成、即将进入 codegen 前插入 mapping_inference
            # （轻量阶段间步骤，不入 Agent 序列；见设计方案 §2.4）
            if "regulation_parser" in current_agents and "codegen" in next_agents:
                paused = await self._mapping_confirmation_gate(
                    state, task_context, completed_outputs, start_time)
                if paused:
                    # 已挂起 waiting_confirmation，等待人工确认后由 confirm-all 恢复
                    return state

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

        # 历史方案库沉淀钩子（范围 D）：容错，沉淀失败绝不阻断任务完成
        await self._settle_solution_case(state)

        return state

    async def _settle_solution_case(self, state: dict):
        """任务完成时把终态摘要沉淀到历史方案库；任何异常仅记日志"""
        try:
            from backend.services import solution_library
            await solution_library.record_case_from_state(state)
        except Exception as e:
            logger.warning("方案案例沉淀失败（不阻断任务）: %s", e)

    async def _mapping_confirmation_gate(self, state: dict, task_context: dict,
                                         completed_outputs: dict, start_time: float) -> bool:
        """mapping_inference 阶段间步骤 + human-in-the-loop 暂停门禁

        契约（docs/映射工作台与场景包设计方案.md §1.4/§2.4）：
        - 调 B 的 mapping_engine.infer_mappings(report_pack, schemas) 推断字段映射并落库；
        - 全部 ≥0.85 且任务 auto_mode=True → 直接放行 codegen（返回 False）；
        - 否则 → status=waiting_confirmation，checkpoint={"next":["codegen"],
          "pause_reason":"mapping_confirmation"}，返回 True 终止本次执行；
        - B/A 依赖未就绪时轻量兜底：跳过推断直接进 codegen 并记日志（保证主链路可跑）。
        """
        # 依赖延迟导入：B（MappingEngine/models）与 A（report_pack_service）并行开发中
        try:
            from backend.services import report_pack_service
            from backend.services.mapping_engine import MappingEngine
            from backend.models.field_mapping import FieldMapping  # noqa: F401
        except Exception as e:
            logger.info("映射推断依赖未就绪（%s），跳过 mapping_inference 直接进入 codegen", e)
            return False

        task_id = state.get("task_id", "")

        # 门禁回退重跑 regulation_parser 时，已有终态映射（人工已确认）→ 不重复推断、不覆盖人工结论
        if await self._has_terminal_mappings(task_id):
            logger.info("任务 %s 已存在终态映射，跳过重复推断", task_id)
            return False

        # 1) 读取场景包（A 的接口：get_pack 返回 dict/None）
        # 仅任务创建时显式指定 report_pack_id 才启用 HITL 映射门禁（入口已快照，
        # 不读 task_context——Agent 1 会回填缺省 G01）；存量任务保持原演示路径
        if not getattr(self, "_mapping_gate_enabled", False):
            logger.info("任务未显式指定场景包，跳过 mapping_inference（存量兼容路径）")
            return False
        pack_id = state.get("report_config", {}).get("report_pack_id") or "G01"
        try:
            report_pack = await report_pack_service.get_pack(pack_id)
        except Exception as e:
            logger.warning("读取场景包 %s 失败（%s），跳过 mapping_inference", pack_id, e)
            return False
        if not report_pack:
            logger.info("场景包 %s 不存在，跳过 mapping_inference", pack_id)
            return False

        # 2) 取候选源表 Schema（复用 database_mcp 只读通道）
        pack_tables = self._pack_attr(report_pack, "source_tables") or []
        source_tables = task_context.get("source_tables") or pack_tables
        schemas = {}
        for table in source_tables:
            try:
                schemas[table] = await self.mcp_services["database_mcp"].query_schema(table_name=table)
            except Exception as e:
                logger.warning("查询源表 %s Schema 失败: %s", table, e)

        # 3) 调 B 的映射推断引擎（MappingEngine 实例；复用 database_mcp 只读通道做画像）
        try:
            engine = MappingEngine(db_mcp=self.mcp_services["database_mcp"])
            inferred = await engine.infer_mappings(report_pack, schemas, task_id=task_id)
        except Exception as e:
            logger.error("映射推断引擎执行失败（%s），兜底直接进入 codegen", e, exc_info=True)
            return False
        mappings = [self._mapping_to_dict(m) for m in (inferred or [])]
        if not mappings:
            logger.info("映射推断无产出，直接进入 codegen")
            return False

        # 4) 映射落库（field_mappings，唯一约束 task_id+target_field 由模型保证）
        await self._persist_mappings(task_id, pack_id, mappings)

        # 5) 分级判定：全部高置信且 auto_mode → 不暂停
        auto_mode = bool(task_context.get("auto_mode", False))
        all_high = all((m.get("confidence") or 0) >= MAPPING_AUTO_THRESHOLD for m in mappings)
        if auto_mode and all_high:
            logger.info("任务 %s 映射全部高置信(≥%.2f)且 auto_mode，直接进入 codegen",
                        task_id, MAPPING_AUTO_THRESHOLD)
            return False

        # 6) 挂起等待人工确认
        state["status"] = "waiting_confirmation"
        state["current_stage"] = "mapping_confirmation"
        state["checkpoint"] = {
            "completed": list(completed_outputs.keys()),
            "next": ["codegen"],
            "pause_reason": "mapping_confirmation",
        }
        state["duration_ms"] = int((time.time() - start_time) * 1000)
        await task_service.save_task_state(state)
        logger.info("任务 %s 挂起 waiting_confirmation（%d 条映射待人工确认）", task_id, len(mappings))
        return True

    @staticmethod
    def _pack_attr(report_pack, key, default=None):
        """场景包属性读取（兼容 ORM 对象与 dict）"""
        if isinstance(report_pack, dict):
            return report_pack.get(key, default)
        return getattr(report_pack, key, default)

    @classmethod
    def _mapping_to_dict(cls, mapping) -> dict:
        """FieldMapping（ORM 对象或 dict）→ 统一 dict，供分级判定与落库"""
        if isinstance(mapping, dict):
            return mapping
        keys = ("target_field", "source_table", "source_field", "transform_rule",
                "confidence", "evidence", "status", "report_pack_id")
        return {k: getattr(mapping, k, None) for k in keys}

    async def _has_terminal_mappings(self, task_id: str) -> bool:
        """任务是否已有人工终态映射（避免回退重跑时覆盖人工结论）"""
        try:
            from sqlalchemy import select, func
            from backend.database import PlatformSessionLocal
            from backend.models.field_mapping import FieldMapping
            async with PlatformSessionLocal() as session:
                cnt = (await session.execute(
                    select(func.count(FieldMapping.id)).where(
                        FieldMapping.task_id == task_id,
                        FieldMapping.status.in_(MAPPING_TERMINAL_STATUS),
                    )
                )).scalar() or 0
            return cnt > 0
        except Exception as e:
            logger.warning("检查终态映射失败（按无终态处理）: %s", e)
            return False

    async def _persist_mappings(self, task_id: str, pack_id: str, mappings: List[dict]):
        """映射推断结果落库 field_mappings（已存在的 target_field 覆盖更新）"""
        from datetime import datetime
        from sqlalchemy import select
        from backend.database import PlatformSessionLocal
        from backend.models.field_mapping import FieldMapping

        async with PlatformSessionLocal() as session:
            for m in mappings:
                target_field = m.get("target_field") or ""
                if not target_field:
                    continue
                existing = (await session.execute(
                    select(FieldMapping).where(
                        FieldMapping.task_id == task_id,
                        FieldMapping.target_field == target_field,
                    )
                )).scalars().first()
                values = {
                    "target_field": target_field,
                    "report_pack_id": m.get("report_pack_id") or pack_id,
                    "source_table": m.get("source_table"),
                    "source_field": m.get("source_field"),
                    "transform_rule": m.get("transform_rule") or "DIRECT",
                    "confidence": m.get("confidence") or 0.0,
                    "evidence": m.get("evidence") or {},
                    # 引擎已按置信度分级（ai_inferred/unmapped），这里尊重引擎结论
                    "status": m.get("status") or "ai_inferred",
                }
                if existing:
                    for k, v in values.items():
                        setattr(existing, k, v)
                    if hasattr(existing, "updated_at"):
                        existing.updated_at = datetime.now()
                else:
                    session.add(FieldMapping(
                        id=f"FM_{uuid.uuid4().hex[:12]}",
                        task_id=task_id,
                        **values,
                    ))
            await session.commit()

    async def _load_confirmed_mappings(self, task_id: str) -> List[dict]:
        """加载任务的人工终态映射（codegen prompt 注入用；依赖未就绪时返回空）"""
        try:
            from sqlalchemy import select
            from backend.database import PlatformSessionLocal
            from backend.models.field_mapping import FieldMapping
            async with PlatformSessionLocal() as session:
                rows = (await session.execute(
                    select(FieldMapping).where(
                        FieldMapping.task_id == task_id,
                        FieldMapping.status.in_(MAPPING_TERMINAL_STATUS),
                    )
                )).scalars().all()
            return [
                {
                    "target_field": r.target_field,
                    "source_table": r.source_table,
                    "source_field": r.source_field,
                    "transform_rule": r.transform_rule,
                    "status": r.status,
                }
                for r in rows
            ]
        except Exception as e:
            logger.info("加载已确认映射失败（按无注入处理）: %s", e)
            return []

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
            # HITL：注入专家已确认的映射（prompt 约束"必须采用"）
            kwargs["confirmed_mappings"] = await self._load_confirmed_mappings(
                task_context.get("task_id", ""))

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
