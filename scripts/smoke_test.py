"""
M1 冒烟测试：3Agent 串行流程验证
不依赖真实 AI API Key（离线 MockAIAdapter）、不依赖 MySQL（SQLite + 内存模拟 Schema）。

验证内容:
  1. backend 包整体可导入
  2. 内嵌制度文本注入租户 RAG
  3. 创建报送任务，走完 制度解析 → 代码生成 → 质量校验 三个阶段
  4. 打印每阶段结果摘要与质量门禁报告

运行方式: python scripts/smoke_test.py
"""

import asyncio
import sys
from pathlib import Path

# 将项目根加入 sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


# 内嵌的小型制度文本（EAST 住房贷款口径节选）
INLINE_REGULATION = """# EAST 个人住房贷款口径（冒烟测试内嵌版）
- 贷款余额：会计账面余额，必须包含利息调整部分（资本化利息），不是纯本金余额
- 逾期本金：逾期90天以内按已逾期部分本金填报；逾期91天及以上按整笔本金填报
- 利率：统一 D20.6，保留6位小数
- 公积金组合贷(product_code='P001-G')纳入住房贷款统计
【严重】贷款余额口径：必须包含利息调整部分，不是纯本金余额
【严重】逾期本金分段：90天是分界点，前后口径完全不同
"""


async def main():
    # 轻量列迁移（老库补新列，与 lifespan 行为一致）
    from backend.services.task_service import ensure_task_columns
    await ensure_task_columns()

    print("=" * 60)
    print("M1 冒烟测试: 3Agent 串行流程（制度解析→代码生成→质量校验）")
    print("=" * 60)

    # 1. 导入验证
    from backend.core.orchestrator import TaskOrchestrator
    from backend.core.tenant_context import PRESET_TENANTS, TenantContext
    from backend.core.ai_adapter import MockAIAdapter
    print("\n[1/4] backend 包导入成功")

    # 2. 设置租户上下文 + 注入内嵌制度文本
    tenant_id = "T001"
    tenant_config = PRESET_TENANTS[tenant_id]
    TenantContext.set_tenant(tenant_id, tenant_config)

    orchestrator = TaskOrchestrator(tenant_id)
    rag = orchestrator.mcp_services["regulation_rag"]
    await rag.add_document("smoke_east_housing", INLINE_REGULATION, "EAST", "EAST_住房贷款口径_冒烟")
    print(f"[2/4] 已注入内嵌制度文本（当前 RAG 文档数: {len(rag.documents)}）")
    print(f"      AI 适配器类型: {type(orchestrator.ai_backend).__name__}")

    # 3. 创建任务并执行
    task_context = {
        "task_id": "TASK_SMOKE_001",
        "tenant_id": tenant_id,
        "report_type": "EAST",
        "report_code": "EAST_LOAN_01",
        "section": "个人住房贷款",
        "source_tables": ["loan_contract"],
        "target_table": "rpt_east_housing_loan",
        "output_mode": "sql",
        "dialect": "mysql"
    }

    result = await orchestrator.execute_task(task_context)
    print(f"[3/4] 任务执行完成: status={result['status']}, "
          f"progress={result['progress']}, duration={result.get('duration_ms')}ms, "
          f"retry={result['retry_count']}")

    # 4. 打印各阶段结果
    print("\n[4/4] 各阶段执行明细:")
    print("-" * 60)
    for stage in result["stages"]:
        name = stage["agent_name"]
        status = stage["status"]
        dur = stage["duration_ms"]
        print(f"  ▶ {name:20s} status={status:8s} {dur}ms")

        out = stage.get("output", {})
        if name == "regulation_parser":
            print(f"      检索到制度: {out.get('retrieved_count', 0)} 条, "
                  f"识别陷阱: {len(out.get('traps_identified', []))} 个")
            for trap in out.get("traps_identified", [])[:2]:
                print(f"        - [{trap['level']}] {trap['description'][:40]}...")
        elif name == "codegen":
            code = out.get("generated_code", "")
            print(f"      生成代码: {len(code)} 字符, 语言={out.get('code_language')}")
            for line in code.splitlines()[:4]:
                print(f"        {line}")
            print("        ...")
        elif name == "quality_gate":
            print(f"      门禁判定: {out.get('gate_result')} "
                  f"(blocker={out.get('blocker_count')}, warning={out.get('warning_count')})")
            for dim_name, dim in out.get("dimensions", {}).items():
                print(f"        {dim_name:22s} {dim['status']}")
            print(f"      结论: {out.get('summary')}")

    print("-" * 60)

    # 断言：3Agent 阶段全部成功，门禁通过
    stage_names = [s["agent_name"] for s in result["stages"]]
    assert "regulation_parser" in stage_names, "缺少制度解析阶段"
    assert "codegen" in stage_names, "缺少代码生成阶段"
    assert "quality_gate" in stage_names, "缺少质量校验阶段"

    gate = result["outputs"].get("quality_gate", {})
    assert gate.get("gate_result") in ("pass", "warn"), f"质量门禁意外阻断: {gate.get('summary')}"
    assert result["status"] == "completed", f"任务未完成: {result.get('error')}"

    # 清理冒烟测试注入的制度文档（M3 起向量索引为持久化，避免污染统计）
    await rag.vector_service.remove_document("smoke_east_housing")

    print("\n✅ 冒烟测试通过: 3Agent 串行流程正常，质量门禁判定 =", gate.get("gate_result"))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
