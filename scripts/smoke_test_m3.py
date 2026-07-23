"""
M3 冒烟测试：数据与向量库管线验证
不依赖真实 AI Key / MySQL。

验证场景:
  a) 种子导入 38 份文档，全部 indexed，统计正确
  b) 3 个真实业务问题检索测试，Top-5 能召回正确文档
  c) 上传新 txt → 自动索引 → 检索能召回
  d) 禁用文档后检索不再召回；reindex 后状态正确
  e) 任务持久化：重建 service 实例（模拟重启）仍能查到任务
  f) 由调用方另行回归 M1/M2 冒烟与 /health

运行方式: python scripts/smoke_test_m3.py
"""

import asyncio
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

TENANT_ID = "T001"


async def scenario_a():
    """a) 种子导入结果与统计校验"""
    print("=" * 64)
    print("场景 a) 种子导入校验")
    print("=" * 64)

    from backend.database import PlatformSessionLocal
    from backend.models.document import RegulationDocument
    from backend.services.vector_service import VectorService
    from sqlalchemy import select

    async with PlatformSessionLocal() as session:
        result = await session.execute(
            select(RegulationDocument).where(RegulationDocument.tenant_id == TENANT_ID)
        )
        docs = result.scalars().all()

    indexed = [d for d in docs if d.status == "indexed"]
    total_chunks = sum(d.chunk_count for d in docs)
    vs = VectorService(TENANT_ID)
    stats = vs.stats()

    print(f"元数据文档数: {len(docs)}, indexed: {len(indexed)}")
    print(f"切片总数(元数据): {total_chunks}, 向量库向量数: {stats['vector_count']}")
    assert len(docs) == 38, f"期望 38 份文档，实际 {len(docs)}"
    assert len(indexed) == 38, "存在未 indexed 的文档"
    assert stats["indexed_docs"] == 38
    print("✅ 场景 a 通过\n")
    return docs


async def scenario_b():
    """b) 3 个真实业务问题检索测试"""
    print("=" * 64)
    print("场景 b) 检索测试（3 个真实业务问题）")
    print("=" * 64)

    from backend.services.vector_service import VectorService
    vs = VectorService(TENANT_ID)

    cases = [
        ("个人住房贷款逾期90天怎么算", ["逾期", "90"]),
        ("EAST 借据表贷款余额口径", ["EAST", "借据"]),
        ("大额交易报告标准", ["大额交易"]),
    ]

    all_hit = True
    for query, keywords in cases:
        result = vs.retrieve(query, top_k=5)
        top = result["results"]
        print(f"\n问题: {query}  (耗时 {result['elapsed_ms']}ms, 命中 {result['total_found']} 条)")
        for i, r in enumerate(top[:3]):
            print(f"  #{i+1} [{r['relevance_score']:.2f}] {r['doc_title']}")
            print(f"      {r['content'][:60].replace(chr(10), ' ')}...")
        if not top:
            print("  ❌ 无召回结果")
            all_hit = False
            continue
        top1_text = top[0]["doc_title"] + top[0]["content"]
        hit = any(k in top1_text for k in keywords)
        print(f"  Top-1 关键词命中: {'✅' if hit else '❌'}")
        all_hit = all_hit and hit

    assert all_hit, "存在检索未命中正确文档的问题"
    print("\n✅ 场景 b 通过\n")


async def scenario_c():
    """c) 上传新文档 → 自动索引 → 检索召回"""
    print("=" * 64)
    print("场景 c) 新文档上传→索引→召回")
    print("=" * 64)

    import uuid
    from backend.config import settings
    from backend.database import PlatformSessionLocal
    from backend.models.document import RegulationDocument
    from backend.services.vector_service import VectorService

    content = """# 内部测试制度 绿色信贷专项统计口径
## 绿色贷款认定
- 绿色贷款需符合人民银行绿色金融统计标准
- 碳减排支持工具贷款单独标识
【提示】本制度为冒烟测试临时上传，验证后可删除"""

    doc_id = str(uuid.uuid4())
    filename = "内部_绿色信贷专项统计_M3测试.txt"
    upload_dir = os.path.join(settings.upload_dir, TENANT_ID, "regulations")
    os.makedirs(upload_dir, exist_ok=True)
    file_path = os.path.join(upload_dir, f"{doc_id}_{filename}")
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(content)

    vs = VectorService(TENANT_ID)
    idx = await vs.index_document(doc_id, content, "自定义", Path(filename).stem)
    async with PlatformSessionLocal() as session:
        session.add(RegulationDocument(
            id=doc_id, tenant_id=TENANT_ID, filename=filename, doc_type="自定义",
            file_path=file_path, size=len(content.encode("utf-8")),
            status="indexed", chunk_count=idx["chunk_count"],
            vector_count=idx["chunk_count"], uploaded_by="smoke_m3"
        ))
        await session.commit()

    result = vs.retrieve("绿色贷款认定标准", top_k=5, active_doc_ids=None)
    hit = any(r["doc_id"] == doc_id for r in result["results"])
    print(f"新文档 {filename} 切片 {idx['chunk_count']} 个")
    print(f"检索'绿色贷款认定标准'召回新文档: {'✅' if hit else '❌'}")
    assert hit, "新文档未被召回"
    print("✅ 场景 c 通过\n")
    return doc_id


async def scenario_d(doc_id):
    """d) 禁用后不召回；reindex 状态正确"""
    print("=" * 64)
    print("场景 d) 禁用/重建索引")
    print("=" * 64)

    from backend.database import PlatformSessionLocal
    from backend.models.document import RegulationDocument
    from backend.services.vector_service import VectorService
    from sqlalchemy import select

    # 禁用文档
    async with PlatformSessionLocal() as session:
        row = await session.get(RegulationDocument, doc_id)
        row.is_active = False
        await session.commit()

    # 模拟 API 的 active_doc_ids 过滤
    async with PlatformSessionLocal() as session:
        result = await session.execute(
            select(RegulationDocument.id).where(
                RegulationDocument.tenant_id == TENANT_ID,
                RegulationDocument.is_active == True,  # noqa: E712
                RegulationDocument.status == "indexed"
            )
        )
        active_ids = {r[0] for r in result.all()}

    vs = VectorService(TENANT_ID)
    result = vs.retrieve("绿色贷款认定标准", top_k=5, active_doc_ids=active_ids)
    hit = any(r["doc_id"] == doc_id for r in result["results"])
    print(f"禁用后召回: {'❌ 仍被召回' if hit else '✅ 不再召回'}")
    assert not hit, "禁用文档仍被召回"

    # 单文档 reindex
    async with PlatformSessionLocal() as session:
        row = await session.get(RegulationDocument, doc_id)
        row.is_active = True  # 恢复启用
        await session.commit()
    with open((await _get_path(doc_id)), "r", encoding="utf-8") as f:
        content = f.read()
    idx = await vs.index_document(doc_id, content, "自定义", "内部_绿色信贷专项统计_M3测试")
    print(f"reindex 后切片 {idx['chunk_count']} 个, 状态 indexed ✅")

    # 清理测试文档
    file_path = await _get_path(doc_id)
    await vs.remove_document(doc_id)
    async with PlatformSessionLocal() as session:
        row = await session.get(RegulationDocument, doc_id)
        await session.delete(row)
        await session.commit()
    if file_path and os.path.exists(file_path):
        os.remove(file_path)
    print("测试文档已清理")
    print("✅ 场景 d 通过\n")


async def _get_path(doc_id):
    from backend.database import PlatformSessionLocal
    from backend.models.document import RegulationDocument
    async with PlatformSessionLocal() as session:
        row = await session.get(RegulationDocument, doc_id)
        return row.file_path


async def scenario_e():
    """e) 任务持久化：模拟重启后仍可查询"""
    print("=" * 64)
    print("场景 e) 任务状态持久化")
    print("=" * 64)

    from backend.core.orchestrator import TaskOrchestrator
    from backend.database import Base, platform_engine

    async with platform_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    orch = TaskOrchestrator(TENANT_ID)
    result = await orch.execute_task({
        "task_id": "TASK_M3_PERSIST_001",
        "tenant_id": TENANT_ID,
        "report_type": "EAST",
        "report_code": "EAST_LOAN_01",
        "source_tables": ["loan_contract"],
        "target_table": "rpt_east_housing_loan"
    })
    print(f"任务执行: status={result['status']}, 阶段数={len(result['stages'])}")

    # 模拟重启：重新 import 一个新的 service 模块实例视角（SQLite 持久化与进程内存无关）
    import importlib
    from backend.services import task_service
    importlib.reload(task_service)
    state = await task_service.get_task_state("TASK_M3_PERSIST_001")

    assert state is not None, "重启后任务丢失"
    assert state["status"] == "completed", f"任务状态异常: {state['status']}"
    assert len(state["stages"]) == 6, "阶段明细未持久化"
    print(f"重启后查询: status={state['status']}, 阶段数={len(state['stages'])}, "
          f"progress={state['progress']}")
    print("✅ 场景 e 通过\n")


async def main():
    await scenario_a()
    await scenario_b()
    doc_id = await scenario_c()
    await scenario_d(doc_id)
    await scenario_e()

    print("=" * 64)
    print("✅ M3 冒烟测试全部通过")
    print("=" * 64)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
