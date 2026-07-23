"""
M2 冒烟测试：6Agent 完整链路验证
不依赖真实 AI API Key（离线 MockAIAdapter）、不依赖 MySQL（SQLite 演示数据集）。

验证场景:
  a) 正向：完整 6Agent 任务跑通，Agent 4/5 并行，Agent 6 生成 Markdown 交付物
  b) 门禁回退：先坏后好的 Mock，验证 block → 回退 Agent 2 → 重试通过；
     以及始终坏的 Mock，验证超过最大重试后任务 failed
  c) 数字孪生：两口径差异量化 + 归因文字

运行方式: python scripts/smoke_test_m2.py
"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# 运行环境隔离（Day10 改造）：所有数据落临时目录，运行后整体清理，不再污染 ./data/
_TMP = tempfile.TemporaryDirectory(prefix="rrp_smoke_m2_")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_TMP.name}/platform.db"
os.environ["UPLOAD_DIR"] = f"{_TMP.name}/tenants"
os.environ["DEMO_DB_PATH"] = f"{_TMP.name}/demo_biz.db"
os.environ["TASK_WORK_DIR"] = f"{_TMP.name}/tasks"
os.environ["LOG_DIR"] = f"{_TMP.name}/logs"
os.environ.setdefault("SECRET_KEY", "smoke-test-secret")
os.environ.setdefault("DEBUG", "false")

# 内嵌制度文本（EAST 住房贷款口径节选）
INLINE_REGULATION = """# EAST 个人住房贷款口径（冒烟测试内嵌版）
- 贷款余额：会计账面余额，必须包含利息调整部分（资本化利息），不是纯本金余额
- 逾期本金：逾期90天以内按已逾期部分本金填报；逾期91天及以上按整笔本金填报
- 利率：统一 D20.6，保留6位小数
- 公积金组合贷(product_code='P001-G')纳入住房贷款统计
【严重】贷款余额口径：必须包含利息调整部分，不是纯本金余额
【严重】逾期本金分段：90天是分界点，前后口径完全不同
"""

TASK_CONTEXT = {
    "task_id": "TASK_M2_SMOKE_001",
    "tenant_id": "T001",
    "report_type": "EAST",
    "report_code": "EAST_LOAN_01",
    "section": "个人住房贷款",
    "source_tables": ["loan_contract"],
    "target_table": "rpt_east_housing_loan",
    "output_mode": "sql",
    "dialect": "mysql"
}

# 故意违规的 SQL：缺 org_no/is_deleted/is_test、余额不含资本化利息、利率未 ROUND 6
BAD_SQL = """```sql
INSERT INTO rpt_east_housing_loan (contract_no, cust_id, loan_balance, execute_rate, overdue_principal, biz_date, org_no)
SELECT contract_no, cust_id, principal_balance, execute_rate, 0, biz_date, org_no FROM loan_contract
```"""


class BadThenGoodMock:
    """先坏后好的 Mock 适配器：前 bad_times 次返回违规SQL，之后返回合规SQL"""

    def __init__(self, good_adapter, bad_times: int = 1):
        self.good_adapter = good_adapter
        self.bad_times = bad_times
        self.calls = 0

    async def chat_completion(self, messages, **kwargs):
        self.calls += 1
        if self.calls <= self.bad_times:
            return {"choices": [{"message": {"role": "assistant", "content": BAD_SQL}}]}
        return await self.good_adapter.chat_completion(messages, **kwargs)


def make_orchestrator(tenant_id: str, adapter=None):
    from backend.core.orchestrator import TaskOrchestrator
    orch = TaskOrchestrator(tenant_id)
    if adapter is not None:
        # 替换编排器与所有 Agent 的 AI 后端
        orch.ai_backend = adapter
        for agent in orch.agents.values():
            agent.set_ai_backend(adapter)
    return orch


async def scenario_a():
    """a) 正向：完整 6Agent 链路"""
    print("=" * 64)
    print("场景 a) 正向: 6Agent 完整链路")
    print("=" * 64)

    from backend.core.tenant_context import PRESET_TENANTS, TenantContext
    TenantContext.set_tenant("T001", PRESET_TENANTS["T001"])

    orch = make_orchestrator("T001")
    rag = orch.mcp_services["regulation_rag"]
    await rag.add_document("m2_east_housing", INLINE_REGULATION, "EAST", "EAST_住房贷款口径_M2")

    ctx = dict(TASK_CONTEXT)
    result = await orch.execute_task(ctx)
    assert result["status"] == "completed", f"任务失败: {result.get('error')}"

    stage_names = [s["agent_name"] for s in result["stages"]]
    print(f"任务状态: {result['status']}, 进度: {result['progress']}, 重试: {result['retry_count']}")
    print(f"阶段序列: {' → '.join(stage_names)}\n")

    for s in result["stages"]:
        name = s["agent_name"]
        out = s["output"]
        if name == "regulation_parser":
            print(f"[Agent1 制度解析] 检索 {out.get('retrieved_count')} 条制度, 陷阱 {len(out.get('traps_identified', []))} 个")
        elif name == "codegen":
            print(f"[Agent2 代码生成] SQL {len(out.get('generated_code', ''))} 字符")
        elif name == "quality_gate":
            print(f"[Agent3 质量校验] 门禁={out.get('gate_result')} "
                  f"(blocker={out.get('blocker_count')}, warning={out.get('warning_count')})")
        elif name == "test_verify":
            print(f"[Agent4 测试验证] 总体={out.get('overall_result')} | {out.get('summary')}")
            for c in out.get("checks", []):
                print(f"    {c['name']:22s} {c['status']:8s} {c.get('detail', '')}")
        elif name == "digital_twin":
            da = out.get("diff_analysis", {})
            print(f"[Agent5 数字孪生] {out.get('scenario')}")
            print(f"    1104总额={out['instance_a']['total_balance']:,.2f}  EAST总额={out['instance_b']['total_balance']:,.2f}")
            print(f"    差异总额={da.get('abs_diff_total'):,.2f} 元 ({da.get('rel_diff_total'):.4%}), "
                  f"差异记录 {da.get('diff_record_count')} 笔, 等级分布 {da.get('level_distribution')}")
        elif name == "deploy":
            print(f"[Agent6 投产交付] {out.get('summary')}")
            for d in out.get("deliverables", []):
                print(f"    {d['filename']}  ({d['size']} bytes)")

    # 验证 4/5 并行：两个阶段在同层，duration 应重叠（简化验证：两者都完成且 deploy 在其后）
    assert stage_names.index("deploy") > stage_names.index("test_verify")
    assert stage_names.index("deploy") > stage_names.index("digital_twin")

    # 验证交付物文件真实存在
    deploy_out = result["outputs"]["deploy"]
    for d in deploy_out["deliverables"]:
        assert os.path.exists(d["path"]), f"交付物不存在: {d['path']}"
    print(f"\n✅ 场景 a 通过：交付物目录 {deploy_out['work_dir']}")
    return result


async def scenario_b():
    """b) 门禁回退：先坏后好 + 始终坏"""
    print("\n" + "=" * 64)
    print("场景 b) 门禁回退: block → 回退 Agent2 → 重试")
    print("=" * 64)

    from backend.core.ai_adapter import MockAIAdapter

    # b1: 第一次生成违规SQL（触发 Agent3 block），重试后合规 → 任务应完成且 retry=1
    good = MockAIAdapter({})
    flaky = BadThenGoodMock(good, bad_times=1)
    orch = make_orchestrator("T001", adapter=flaky)
    ctx = dict(TASK_CONTEXT, task_id="TASK_M2_RETRY_OK")
    result = await orch.execute_task(ctx)
    stages = [s["agent_name"] for s in result["stages"]]
    print(f"b1 先坏后好: status={result['status']}, retry={result['retry_count']}, "
          f"AI调用次数={flaky.calls}")
    print(f"   阶段序列: {' → '.join(stages)}")
    assert result["status"] == "completed" and result["retry_count"] == 1
    assert stages.count("codegen") == 2 and stages.count("quality_gate") == 2
    print("   ✅ block 后正确回退 Agent2 重试并通过")

    # b2: 始终违规 → 超过最大重试 → failed
    always_bad = BadThenGoodMock(good, bad_times=99)
    orch2 = make_orchestrator("T001", adapter=always_bad)
    ctx2 = dict(TASK_CONTEXT, task_id="TASK_M2_RETRY_FAIL")
    result2 = await orch2.execute_task(ctx2)
    print(f"\nb2 始终违规: status={result2['status']}, retry={result2['retry_count']}, "
          f"error={result2.get('error')}")
    assert result2["status"] == "failed"
    assert result2["retry_count"] == orch2.MAX_GATE_RETRY + 1
    print("   ✅ 超过最大重试次数后任务正确失败")


async def scenario_c(result_a):
    """c) 数字孪生差异验证"""
    print("\n" + "=" * 64)
    print("场景 c) 数字孪生: 差异量化与归因")
    print("=" * 64)

    twin = result_a["outputs"]["digital_twin"]
    da = twin["diff_analysis"]
    attr = twin["attribution"]

    # 差异总额应等于种子数据中住房贷款资本化利息之和
    # C001 1200 + C003 5600 + C004 2300 + C007 800 + C008 1500 + C012 4200 = 15600
    expected_diff = 15600.0
    print(f"差异总额: {da['abs_diff_total']:,.2f} 元（期望 {expected_diff:,.2f}）")
    print(f"相对差异: {da['rel_diff_total']:.4%}")
    print(f"差异记录: {da['diff_record_count']} 笔 / 一致 {da['match_record_count']} 笔")
    print(f"归因方向: {attr['direction']}")
    print(f"归因结论: {attr['conclusion']}")
    print(f"对账建议: {attr['suggestion']}")
    print("Top 差异样例:")
    for s in da["top_diff_samples"][:3]:
        print(f"    {s['contract_no']}: 1104={s['value_1104']:,.2f} EAST={s['value_east']:,.2f} "
              f"Δ={s['abs_diff']:,.2f} ({s['rel_diff']:.4%}, {s['diff_level']})")

    assert abs(da["abs_diff_total"] - expected_diff) < 0.01, "差异总额与种子数据不符"
    assert da["diff_record_count"] == 6, "差异记录数应为 6（两笔无资本化利息）"
    assert attr["reasons"], "缺少归因说明"
    print("\n✅ 场景 c 通过：差异量化正确，归因完整")


async def main():
    from backend.core import orchestrator as _  # 触发包导入
    from backend.services import task_service
    from backend.database import Base, platform_engine

    # 全新临时库：先建表，再做轻量列迁移（老库补新列）
    async with platform_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await task_service.ensure_task_columns()  # 轻量列迁移（老库补新列）

    result_a = await scenario_a()
    await scenario_b()
    await scenario_c(result_a)

    # 任务状态登记验证（供 API 查询）
    state = await task_service.get_task_state("TASK_M2_SMOKE_001")
    assert state and state["status"] == "completed"
    print(f"\n任务状态登记: TASK_M2_SMOKE_001 → {state['status']}, "
          f"阶段数 {len(state['stages'])}")

    # 清理冒烟测试注入的制度文档（M3 起向量索引为持久化，避免污染统计）
    from backend.services.vector_service import VectorService
    await VectorService("T001").remove_document("m2_east_housing")

    print("\n" + "=" * 64)
    print("✅ M2 冒烟测试全部通过")
    print("=" * 64)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    finally:
        _TMP.cleanup()  # 清理临时数据目录
