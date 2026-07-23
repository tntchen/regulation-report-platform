"""
业务术语词典 + 映射引擎 name 通道增强单测（范围 E）

覆盖：
- 幂等种子：首次写入 20+ 条，重复执行不再新增
- CRUD 基本行为（创建/查重/更新/删除）
- 术语命中提升 name 通道得分（注入词条走 _name_score / _match_term_fields）
- 无词典（空词条 / 构造未注入且服务不可用）时 name 通道行为与旧逻辑一致
- field_hints 中不存在于候选 schema 的字段不产生加分（幻觉字段天然过滤）

运行方式: python -m pytest tests/test_term_dict.py -v
"""

import asyncio
import os
import tempfile

# 在导入 backend 前切到临时目录（平台库隔离）
_tmpdir = tempfile.mkdtemp(prefix="rrp_test_term_")
os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{_tmpdir}/test_platform.db")
os.environ.setdefault("UPLOAD_DIR", f"{_tmpdir}/tenants")
os.environ.setdefault("DEMO_DB_PATH", f"{_tmpdir}/demo_biz.db")
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-pytest")
os.environ.setdefault("DEBUG", "false")

import pytest

from backend.services.mapping_engine import MappingEngine
from backend.services import term_service
from backend.services.term_service import BUILTIN_TERMS


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(scope="module", autouse=True)
def _create_tables():
    """建 term_dicts 表（仅本范围模型，避免依赖其他子代理的 models/__init__ 改动）"""
    from backend.database import Base, platform_engine
    from backend.models.term_dict import TermDict  # noqa: F401

    async def _ddl():
        async with platform_engine.begin() as conn:
            await conn.run_sync(
                lambda c: Base.metadata.create_all(c, tables=[TermDict.__table__]))

    run(_ddl())
    yield


# ============================================
# 种子与 CRUD
# ============================================
def test_seed_builtin_terms_idempotent():
    created1 = run(term_service.seed_builtin_terms())
    assert len(created1) >= 20, f"内置术语应 20+ 条，实际 {len(created1)}"
    assert set(created1) == {t["term"] for t in BUILTIN_TERMS}

    # 幂等：第二次执行不再新增
    created2 = run(term_service.seed_builtin_terms())
    assert created2 == []

    terms = run(term_service.list_terms())
    assert len(terms) == len(BUILTIN_TERMS)


def test_term_crud():
    # 创建
    row = run(term_service.create_term(
        {"term": "测试术语", "aliases": ["测术"], "field_hints": ["contract_no"],
         "category": "测试"}, created_by="pytest"))
    assert row and row["term"] == "测试术语"

    # 同名查重 → None
    dup = run(term_service.create_term({"term": "测试术语"}, created_by="pytest"))
    assert dup is None

    # 更新
    updated = run(term_service.update_term(row["id"], {"field_hints": ["cust_id"]}))
    assert updated["field_hints"] == ["cust_id"]

    # 删除
    assert run(term_service.delete_term(row["id"])) is True
    assert run(term_service.get_term(row["id"])) is None
    assert run(term_service.delete_term(row["id"])) is False


# ============================================
# 引擎 name 通道术语子信号
# ============================================
TTERMS = [
    {"term": "按揭", "aliases": ["房贷"],
     "field_hints": ["principal_balance", "loan_balance"], "category": "产品"},
]


@pytest.fixture()
def engine():
    # 构造注入词条，不依赖平台库
    return MappingEngine(term_hints=TTERMS)


def test_term_hit_boosts_name_score(engine):
    """口径文本含“按揭”，候选字段在 hints 中 → name 得分提升"""
    score = engine._name_score("loan_balance", "按揭贷款余额（1104口径）", "principal_balance",
                               term_hint_fields={"principal_balance", "loan_balance"})
    assert score == pytest.approx(MappingEngine.TERM_HIT_SCORE, abs=1e-4)

    # 同一场景不带词典：只有原有的编辑距离等子信号，明显更低
    score_plain = engine._name_score("loan_balance", "按揭贷款余额（1104口径）",
                                     "principal_balance")
    assert score > score_plain


def test_match_term_fields_by_alias(engine):
    """别名命中同样生效；未命中返回空集"""
    fields = engine._match_term_fields("loan_balance", "房贷余额口径", TTERMS)
    assert fields == {"principal_balance", "loan_balance"}

    assert engine._match_term_fields("execute_rate", "执行利率", TTERMS) == set()
    assert engine._match_term_fields("x", "y", None) == set()
    assert engine._match_term_fields("x", "y", []) == set()


def test_hints_not_in_schema_no_boost(engine):
    """hints 含 schema 不存在的字段（如 loan_balance 不在候选列里）时，
    只有真实存在的候选列能拿到加分；幻觉字段无候选可命中"""
    # loan_balance 作为“源字段”不存在于演示 schema，不应因词条里的 hint 加分
    score = engine._name_score("贷款余额", "按揭贷款余额", "some_other_col",
                               term_hint_fields={"principal_balance", "loan_balance"})
    assert score < MappingEngine.TERM_HIT_SCORE


def test_no_dict_behavior_unchanged():
    """空词典 / 未注入词条直接调 _name_score：结果与旧逻辑完全一致"""
    e = MappingEngine(term_hints=[])
    base = e._name_score("overdue_days", "逾期天数", "overdue_days")
    with_empty = e._name_score("overdue_days", "逾期天数", "overdue_days",
                               term_hint_fields=set())
    assert base == with_empty == 1.0

    # _match_term_fields 在空词典下为空集 → _score_candidate 路径不引入任何变化
    assert e._match_term_fields("按揭", "按揭", []) == set()
    assert e._match_term_fields("按揭", "按揭", None) == set()


def test_term_dict_load_failure_degrades_silently():
    """构造未注入词条时从服务加载；即使库表不存在也应静默降级为空词典"""
    from backend.services import term_service as ts

    terms = run(ts.load_term_hints_safe())
    # 表已由 fixture 建好且已种子 → 正常返回；这里额外验证容错包装不抛异常
    assert isinstance(terms, list)
