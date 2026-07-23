"""
端到端演示补充种子脚本（范围 E）
依赖前四个种子已跑：seed_tenants / seed_regulations / seed_report_packs / seed_terms。

产出（全部幂等，可重复执行）：
  ① 制度新旧两版演示文档对：写入 data/tenants/T001/regulations/ 并入库索引
     （1104_G11_五级分类_2024版.txt / 1104_G11_五级分类_2025修订版.txt，
      2025 版口径差异：逾期整笔临界点 90→91 天、新增关注类细分条款、删除核销次月排除条款）
  ② 预造一个 completed 的 G11 任务（离线跑编排器，方案库推荐/台账关联/接口文件导出演示有数据）
  ③ 生成当月报送台账（ledger_service.generate_ledger，未就绪则跳过提示）并把 G11 条目绑定演示任务
  ④ 新旧回归演示 SQL 写入 data/tasks/demo_regression/（old.sql/new.sql，差异可解释）
  ⑤ 向演示业务库幂等补充 2 笔逾期 90/91 天临界借据（让 90→91 口径回归有非零差异）

运行方式: python scripts/seed_demo_extra.py
"""

import asyncio
import os
import sqlite3
import sys
import uuid
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

TENANT_ID = "T001"
DEMO_TASK_ID = "TASK_DEMO_G11_001"
DEMO_USER = "demo"

# 演示文档对（源文件在 data/tenants/T001/regulations/ 下，demo_ 前缀为源文件，
# 入库时按平台惯例复制为 {doc_id}_{正式文件名}）
DOC_PAIRS = [
    ("demo_1104_G11_五级分类_2024版.txt", "1104_G11_五级分类_2024版.txt"),
    ("demo_1104_G11_五级分类_2025修订版.txt", "1104_G11_五级分类_2025修订版.txt"),
]

# 回归演示 SQL（差异：不良整笔认定临界点 90→91 天 + 关注类细分标签）
OLD_SQL = """-- G11 五级分类不良余额 · 旧逻辑（2024版口径）
-- 口径：逾期 90 天及以上即按整笔本金填报不良（临界点 90）
SELECT
    contract_no,
    cust_id,
    principal_balance + IFNULL(interest_capitalized, 0) AS loan_balance,
    five_classify,
    overdue_days,
    CASE
        WHEN overdue_days >= 90 THEN principal_balance
        WHEN overdue_days > 0  THEN ROUND(principal_balance * 0.15, 2)  -- 已逾期部分本金（演示按 15% 估算）
        ELSE 0
    END AS npl_amount,
    biz_date,
    org_no
FROM loan_contract
WHERE is_deleted = 0 AND is_test = 0 AND org_no = '1001';
"""

NEW_SQL = """-- G11 五级分类不良余额 · 新逻辑（2025修订版口径）
-- 口径变化：临界点由 90 天调整为 91 天（90 天仍按已逾期部分填报）；
-- 同时按新增关注类细分条款补充分类标签。
SELECT
    contract_no,
    cust_id,
    principal_balance + IFNULL(interest_capitalized, 0) AS loan_balance,
    five_classify,
    overdue_days,
    CASE
        WHEN overdue_days >= 91 THEN principal_balance
        WHEN overdue_days > 0  THEN ROUND(principal_balance * 0.15, 2)  -- 已逾期部分本金（演示按 15% 估算）
        ELSE 0
    END AS npl_amount,
    CASE
        WHEN five_classify = '2' AND overdue_days > 0 THEN '关注二'
        WHEN five_classify = '2' THEN '关注一'
        ELSE five_classify
    END AS classify_detail,
    biz_date,
    org_no
FROM loan_contract
WHERE is_deleted = 0 AND is_test = 0 AND org_no = '1001';
"""

# 逾期 90/91 天临界借据（幂等 INSERT OR IGNORE）：让 90→91 口径回归有非零差异
BOUNDARY_ROWS = [
    # contract_no, cust_id, product_code, loan_amount, principal_balance, interest_capitalized,
    # execute_rate, loan_status, repay_date, overdue_days, five_classify, biz_date, org_no, is_deleted, is_test
    ("C901", "U901", "P001", 800000, 500000, 1000.00, 4.600000, "02", "2026-05-10", 90, "2",
     "2026-07-21", "1001", 0, 0),
    ("C902", "U902", "P001", 900000, 650000, 1500.00, 4.700000, "02", "2026-05-12", 91, "2",
     "2026-07-21", "1001", 0, 0),
]

# 手工 G11 转换 SQL（离线 Mock 适配器只会生成 G01 形态固定 SQL，缺 five_classify 列，
# 无法通过 G11 场景包的 SUM_BY(five_classify, ...) 分组勾稽；此处按 G11 target_schema
# 手工提供 codegen 产出，从 quality_gate 断点续跑，后续 Agent 仍真实执行）
DEMO_G11_SQL = """INSERT INTO rpt_g11_five_classify (
    contract_no,        -- 合同编号，直接取数
    cust_id,            -- 客户ID，直接取数
    loan_balance,       -- 贷款余额 = 本金余额 + 资本化利息
    five_classify,      -- 五级分类：1正常/2关注/3次级/4可疑/5损失
    overdue_days,       -- 逾期天数：逾期90天以上应至少降为次级
    overdue_principal,  -- 逾期本金：90天以内按已逾期部分，91天及以上按整笔本金
    biz_date,           -- 业务日期
    org_no              -- 机构号（权限过滤字段）
)
SELECT
    t.contract_no,
    t.cust_id,
    ROUND(t.principal_balance + IFNULL(t.interest_capitalized, 0), 4),
    t.five_classify,
    IFNULL(t.overdue_days, 0),
    CASE
        WHEN IFNULL(t.overdue_days, 0) >= 91 THEN ROUND(t.principal_balance, 4)  -- 91天及以上按整笔本金
        WHEN IFNULL(t.overdue_days, 0) > 0 THEN ROUND(t.principal_balance, 4)    -- 90天以内按已逾期部分（演示取整笔）
        ELSE 0
    END,
    t.biz_date,
    t.org_no
FROM loan_contract t
WHERE t.is_deleted = 0
  AND t.is_test = 0
  AND t.org_no = '1001'
  AND t.product_code IN ('P001', 'P001-G')
;"""


async def step_docs():
    """① 制度新旧两版演示文档对：落盘 + 元数据 + 向量索引（幂等）"""
    from backend.config import settings
    from backend.database import PlatformSessionLocal
    from backend.models.document import RegulationDocument
    from backend.services.vector_service import VectorService
    from sqlalchemy import select

    print("=" * 64)
    print("[1/5] 制度新旧两版演示文档对")
    print("=" * 64)
    vs = VectorService(TENANT_ID)
    upload_dir = os.path.join(settings.upload_dir, TENANT_ID, "regulations")

    for src_name, formal_name in DOC_PAIRS:
        async with PlatformSessionLocal() as session:
            existing = (await session.execute(
                select(RegulationDocument).where(
                    RegulationDocument.tenant_id == TENANT_ID,
                    RegulationDocument.filename == formal_name,
                )
            )).scalars().first()
        if existing:
            print(f"  [跳过] {formal_name}（已入库，doc_id={existing.id}）")
            continue

        src_path = os.path.join(upload_dir, src_name)
        content = Path(src_path).read_text(encoding="utf-8")
        doc_id = str(uuid.uuid4())
        dest_path = os.path.join(upload_dir, f"{doc_id}_{formal_name}")
        with open(dest_path, "w", encoding="utf-8") as f:
            f.write(content)

        idx = await vs.index_document(doc_id, content, "1104", Path(formal_name).stem)
        async with PlatformSessionLocal() as session:
            session.add(RegulationDocument(
                id=doc_id, tenant_id=TENANT_ID, filename=formal_name,
                doc_type="1104", file_path=dest_path,
                size=len(content.encode("utf-8")), status="indexed",
                chunk_count=idx["chunk_count"], vector_count=idx["chunk_count"],
                uploaded_by=DEMO_USER,
            ))
            await session.commit()
        print(f"  [成功] {formal_name} 切片={idx['chunk_count']} doc_id={doc_id}")


async def step_task():
    """② 预造 completed 的 G11 演示任务（离线跑编排器，幂等）"""
    from backend.services import task_service
    from backend.database import PlatformSessionLocal
    from backend.models.task import Task

    print("=" * 64)
    print("[2/5] G11 演示任务（completed）")
    print("=" * 64)
    existing = await task_service.get_task_state(DEMO_TASK_ID)
    if existing and existing.get("status") == "completed":
        print(f"  [跳过] {DEMO_TASK_ID} 已 completed，阶段数={len(existing.get('stages', []))}")
        return

    from backend.core.orchestrator import TaskOrchestrator
    task_context = {
        "task_id": DEMO_TASK_ID,
        "tenant_id": TENANT_ID,
        "report_type": "1104",
        "report_code": "G11",
        "report_pack_id": "G11",
        "source_tables": ["loan_contract"],
        "target_table": "rpt_g11_five_classify",
        "output_mode": "sql",
        "dialect": "mysql",
        "auto_mode": True,          # 映射全部高置信则免人工确认直接放行
        "created_by": DEMO_USER,    # 方案库案例溯源用
    }
    orch = TaskOrchestrator(TENANT_ID)
    state = await orch.execute_task(task_context)
    print(f"  首次执行: status={state['status']}")

    # auto_mode 未放行（存在低置信映射）→ 模拟人工全部确认后断点续跑
    if state.get("status") == "waiting_confirmation":
        from backend.models.field_mapping import FieldMapping
        from sqlalchemy import select
        async with PlatformSessionLocal() as session:
            rows = (await session.execute(
                select(FieldMapping).where(FieldMapping.task_id == DEMO_TASK_ID)
            )).scalars().all()
            for m in rows:
                if m.status not in ("confirmed", "modified", "needs_etl"):
                    m.status = "confirmed"
                    m.confirmed_by = DEMO_USER
                    m.confirmed_at = datetime.now()
            await session.commit()
        print(f"  映射人工确认 {len(rows)} 条，断点续跑...")
        state = await task_service.get_task_state(DEMO_TASK_ID)
        state = await orch.execute_task(task_context, resume_state=state)
        print(f"  续跑结果: status={state['status']}")

    if state.get("status") != "completed":
        # 离线 Mock 适配器兜底：mock 只生成 G01 形态固定 SQL，过不了 G11 分组勾稽。
        # 用真实跑出的 regulation_parser / codegen 产出，替换为手工 G11 SQL 后
        # 从 quality_gate 断点续跑（后续 Agent 真实执行，结果真实可复核）
        print(f"  Mock 链路未完成（{state.get('error')}），启用断点续跑兜底...")
        state = await _resume_with_manual_sql(orch, task_context)
        print(f"  断点续跑结果: status={state['status']}")

    if state.get("status") != "completed":
        print(f"  ⚠️ 任务未完成（status={state.get('status')} error={state.get('error')}），"
              f"方案库/台账演示数据可能缺失")
        return

    # created_by 溯源：编排器链路不透传 created_by 时直接补写任务行
    async with PlatformSessionLocal() as session:
        row = await session.get(Task, DEMO_TASK_ID)
        if row and not row.created_by:
            row.created_by = DEMO_USER
            await session.commit()
    print(f"  [成功] {DEMO_TASK_ID} completed，阶段数={len(state.get('stages', []))}，"
          f"耗时={state.get('duration_ms')}ms")


async def _resume_with_manual_sql(orch, task_context):
    """Mock 兜底：保留真实 regulation_parser 产出，codegen 替换为手工 G11 SQL，
    构造断点（next=quality_gate）续跑。仅演示种子使用，不改任何业务代码。"""
    from backend.services import task_service

    prev = await task_service.get_task_state(DEMO_TASK_ID) or {}
    prev_outputs = prev.get("outputs") or {}
    reg_output = prev_outputs.get("regulation_parser") or {}
    codegen_output = dict(prev_outputs.get("codegen") or {})
    codegen_output["generated_code"] = DEMO_G11_SQL
    codegen_output["report_pack_id"] = "G11"

    # 复用前两次真实阶段记录（保持阶段明细连续）
    prev_stages = [s for s in (prev.get("stages") or [])
                   if (s.get("agent_name") or s.get("agent")) in ("regulation_parser", "codegen")][:2]

    resume_state = {
        "task_id": DEMO_TASK_ID,
        "tenant_id": TENANT_ID,
        "task_type": "1104",
        "name": "1104 G11 报送任务",
        "status": "executing",
        "current_stage": "codegen",
        "progress": 35,
        "stages": prev_stages,
        "outputs": {"regulation_parser": reg_output, "codegen": codegen_output},
        "report_config": task_context,
        "retry_count": 0,
        "created_by": DEMO_USER,
        "checkpoint": {"completed": ["regulation_parser", "codegen"],
                       "next": ["quality_gate"]},
    }
    return await orch.execute_task(task_context, resume_state=resume_state)


async def step_ledger():
    """③ 当月报送台账 + G11 条目绑定演示任务（范围 A 未就绪则跳过）"""
    print("=" * 64)
    print("[3/5] 当月报送台账")
    print("=" * 64)
    try:
        from backend.services import ledger_service
    except Exception as e:
        print(f"  [跳过] 台账服务未就绪（范围 A 未完成）: {e}")
        return

    period = datetime.now().strftime("%Y-%m")
    try:
        result = await ledger_service.generate_ledger(TENANT_ID, period)
        print(f"  台账生成 period={period} created={result['created']} skipped={result['skipped']}")

        # G11 条目绑定演示任务（台账关联演示）
        entries = await ledger_service.list_ledger(TENANT_ID, period)
        g11 = next((e for e in entries if e["report_pack_id"] == "G11"), None)
        if g11 and not g11.get("task_id"):
            bound = await ledger_service.bind_task(g11["id"], TENANT_ID, DEMO_TASK_ID)
            print(f"  G11 条目绑定任务 {DEMO_TASK_ID}: status={bound['status']}")
        elif g11:
            print(f"  [跳过] G11 条目已绑定任务 {g11['task_id']}")
    except Exception as e:
        print(f"  [跳过] 台账生成失败: {e}")


def step_regression_sql():
    """④ 新旧回归演示 SQL（固定内容覆盖写，幂等）"""
    print("=" * 64)
    print("[4/5] 回归演示 SQL")
    print("=" * 64)
    out_dir = PROJECT_ROOT / "data" / "tasks" / "demo_regression"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "old.sql").write_text(OLD_SQL, encoding="utf-8")
    (out_dir / "new.sql").write_text(NEW_SQL, encoding="utf-8")
    print(f"  [成功] {out_dir}/old.sql, new.sql（差异：整笔认定临界点 90→91 天 + 关注类细分）")


def step_boundary_rows():
    """⑤ 演示业务库补充 90/91 天临界借据（幂等）"""
    print("=" * 64)
    print("[5/5] 逾期临界演示借据（demo_biz.db）")
    print("=" * 64)
    from backend.config import settings
    from backend.mcp.demo_dataset import demo_dataset
    demo_dataset.ensure_seeded()  # 库不存在时先按内置种子初始化

    conn = sqlite3.connect(settings.demo_db_path)
    try:
        cur = conn.cursor()
        cur.executemany(
            "INSERT OR IGNORE INTO loan_contract VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            BOUNDARY_ROWS,
        )
        conn.commit()
        count = cur.execute(
            "SELECT COUNT(*) FROM loan_contract WHERE contract_no IN ('C901','C902')"
        ).fetchone()[0]
        print(f"  [成功] 临界借据 C901(90天)/C902(91天) 已就位（{count}/2 笔）")
    finally:
        conn.close()


async def main():
    # 建表 + 轻量列迁移（与 lifespan 行为一致；台账等新表若模型已注册也一并建）
    from backend.database import Base, platform_engine
    from backend import models  # noqa: F401  确保全部模型注册到 metadata
    from backend.services.task_service import ensure_task_columns
    async with platform_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await ensure_task_columns()

    await step_docs()
    await step_task()
    await step_ledger()
    step_regression_sql()
    step_boundary_rows()

    print("=" * 64)
    print("✅ 演示补充种子完成（可重复执行，幂等）")
    print("=" * 64)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
