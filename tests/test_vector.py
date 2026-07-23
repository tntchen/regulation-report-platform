"""
真实向量检索与索引一致性测试（L2-D8）
覆盖：同义表达召回（语义通道价值）、禁用文档不再被 Agent 1 链路召回（一致性回归）、
     并发索引不丢数据、双通道融合排序字段与顺序

运行方式: python -m pytest tests/test_vector.py -v
注意: local provider 需要 BGE 模型（首次自动下载）；模型不可用时自动降级 tfidf，
     同义召回用例在 tfidf 降级下可能失败（如实反映语义能力缺失）
"""

import asyncio
import os
import tempfile

# 测试环境离线加载模型（已缓存；避免 HF Hub 网络探测拖慢/卡死测试）
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

# 在导入 backend 前切换独立临时目录（避免污染开发数据）
_tmpdir = tempfile.mkdtemp(prefix="rrp_test_vec_")
os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{_tmpdir}/test_platform.db")
os.environ.setdefault("UPLOAD_DIR", f"{_tmpdir}/tenants")
os.environ.setdefault("DEMO_DB_PATH", f"{_tmpdir}/demo_biz.db")
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-pytest")
os.environ.setdefault("DEBUG", "false")

import pytest
import pytest_asyncio

from backend.database import Base, platform_engine, PlatformSessionLocal
from backend.models.document import RegulationDocument
from backend.services.vector_service import VectorService
from backend.services.embedding_service import embedding_service
from backend.mcp.regulation_rag import RegulationRAGService

TENANT = "T009"

DOC_OVERDUE = """# EAST 逾期本金口径
## 逾期本金
- 按月分期还款的个人消费贷款，逾期90天以内按已逾期部分本金余额填报
- 逾期91天及以上按整笔贷款本金余额填报
- 90天是临界点"""

DOC_RATE = """# 利率报备 执行利率
- 执行利率以 LPR 为定价基准，保留 6 位小数
- 浮动区间 = 执行利率 - 对应期限 LPR"""


@pytest_asyncio.fixture
async def vs():
    async with platform_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    service = VectorService(TENANT)
    await service.index_document("doc_overdue", DOC_OVERDUE, "EAST", "EAST_逾期本金口径")
    await service.index_document("doc_rate", DOC_RATE, "利率报备", "利率报备_执行利率")
    return service


@pytest.mark.asyncio
async def test_synonym_recall(vs):
    """同义表达"房贷逾期三个月"应召回"逾期90天"制度（语义通道直接证据）"""
    r = await vs.retrieve("房贷逾期三个月以上怎么算", top_k=2)
    assert r["results"], "无任何召回"
    top = r["results"][0]
    assert top["doc_id"] == "doc_overdue"
    # 语义价值：文本通道几乎打不出分，向量通道撑起召回
    assert top["vector_score"] > 0.5
    assert top["text_score"] < top["vector_score"]


@pytest.mark.asyncio
async def test_dual_channel_fields(vs):
    """融合检索返回向量分/文本分/融合分，且按融合分降序"""
    r = await vs.retrieve("逾期本金", top_k=5)
    assert r["results"]
    for item in r["results"]:
        assert {"vector_score", "text_score", "relevance_score"} <= set(item)
    scores = [x["relevance_score"] for x in r["results"]]
    assert scores == sorted(scores, reverse=True)


@pytest.mark.asyncio
async def test_concurrent_index_no_loss():
    """并发索引不丢数据：5 个文档并发写入，切片全部保留"""
    service = VectorService("T010")
    docs = [(f"d{i}", f"# 文档{i}\n第 {i} 份制度内容，逾期{i}天的规则说明。") for i in range(5)]
    await asyncio.gather(*[
        service.index_document(doc_id, content, "EAST", f"制度_{doc_id}")
        for doc_id, content in docs
    ])
    chunks = await service.load_chunks()
    assert {c["doc_id"] for c in chunks} == {d[0] for d in docs}
    assert len(chunks) == 5  # 每文档 1 切片


@pytest.mark.asyncio
async def test_disabled_doc_not_recalled(vs):
    """索引一致性：禁用文档不再被 RAG 默认链路召回（Agent 1 修复回归）"""
    # 登记元数据：doc_overdue 禁用，doc_rate 启用
    async with PlatformSessionLocal() as session:
        session.add_all([
            RegulationDocument(id="doc_overdue", tenant_id=TENANT, filename="overdue.txt",
                               doc_type="EAST", file_path="", size=1,
                               status="indexed", is_active=False, uploaded_by="test"),
            RegulationDocument(id="doc_rate", tenant_id=TENANT, filename="rate.txt",
                               doc_type="利率报备", file_path="", size=1,
                               status="indexed", is_active=True, uploaded_by="test"),
        ])
        await session.commit()

    rag = RegulationRAGService(TENANT)
    r = await rag.retrieve("逾期90天 逾期本金", top_k=5)
    doc_ids = {x["doc_id"] for x in r["results"]}
    assert "doc_overdue" not in doc_ids, "禁用文档仍被召回"
    assert "doc_rate" in doc_ids


@pytest.mark.asyncio
async def test_removed_doc_gone(vs):
    """删除文档后切片同步移除"""
    service = VectorService("T011")
    await service.index_document("tmp_doc", "# 临时制度\n临时内容", "EAST", "临时")
    assert await service.remove_document("tmp_doc") == 1
    assert await service.load_chunks() == []


@pytest.mark.asyncio
async def test_embedding_async_and_dim():
    """embedding 异步可用，维度与配置一致（512）"""
    v = await embedding_service.embed("测试文本")
    assert len(v) == embedding_service.dimension
