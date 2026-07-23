"""
映射引擎 + 数据画像单测（范围 B，设计方案 §2.2/§2.3）

覆盖：
- 五通道各自打分（name 缩写词典 / comment BGE 余弦 / profile 类型+值域 / semantic / history）
- 注释缺失时通道为 None、融合权重自动重分配
- 置信度分级边界（0.85 / 0.5）
- 画像统计在 SQLite 演示数据集上的正确性（null_rate/distinct/枚举/格式识别）
- 历史映射资产命中复用（注入列表 + 平台库查询两条路径）

embedding 用确定性假向量注入，不加载真实模型；画像测试用独立临时演示库，
不触碰共享 demo_dataset 单例。

运行方式: python -m pytest tests/test_mapping_engine.py -v
"""

import asyncio
import hashlib
import os
import tempfile

import numpy as np

# 在导入 backend 前切到临时目录（平台库与演示库均隔离）
_tmpdir = tempfile.mkdtemp(prefix="rrp_test_mapping_")
os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{_tmpdir}/test_platform.db")
os.environ.setdefault("UPLOAD_DIR", f"{_tmpdir}/tenants")
os.environ.setdefault("DEMO_DB_PATH", f"{_tmpdir}/demo_biz.db")
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-pytest")
os.environ.setdefault("DEBUG", "false")

import pytest

from backend.models.field_mapping import FieldMapping, MappingStatus
from backend.models.mapping_asset import MappingAsset
from backend.services.mapping_engine import (
    MappingEngine, DEFAULT_WEIGHTS, HIGH_CONFIDENCE, MIN_CONFIDENCE,
)
from backend.services.profiling_service import ProfilingService, profile_summary_text


# ============================================
# 确定性假 embedding：按字符 bigram 哈希到 64 维，共享词越多余弦越高
# ============================================
DIM = 64


def _fake_vec(text: str) -> np.ndarray:
    v = np.zeros(DIM, dtype=np.float32)
    text = text.lower()
    grams = [text[i:i + 2] for i in range(len(text) - 1)] or [text]
    for g in grams:
        idx = int(hashlib.md5(g.encode()).hexdigest(), 16) % DIM
        v[idx] += 1.0
    norm = np.linalg.norm(v)
    return v / norm if norm else v


async def fake_embed(text: str):
    return _fake_vec(text).tolist()


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def engine():
    e = MappingEngine(embed_fn=fake_embed)
    return e


def _schema(columns):
    return {"loan_contract": {"columns": columns}}


def _pack(target_schema, pack_id="G11"):
    return {"id": pack_id, "target_schema": target_schema,
            "source_tables": ["loan_contract"]}


# ============================================
# 通道1 name：编辑距离 + 缩写词典
# ============================================
class TestNameChannel:
    def test_abbrev_dict_full_hit(self, engine):
        # principal→本金、bal→余额，全部命中目标字段
        score = engine._name_score("本金余额", "客户贷款本金余额", "principal_bal")
        assert score == 1.0

    def test_abbrev_dict_amt(self, engine):
        score = engine._name_score("贷款金额", "", "loan_amt")
        assert score == 1.0  # loan→贷款、amt→金额 全命中

    def test_partial_hit(self, engine):
        score = engine._name_score("逾期天数", "", "overdue_days")
        assert score == 1.0  # overdue→逾期、days→天数
        low = engine._name_score("逾期天数", "", "product_code")
        assert low < 0.5

    def test_identical_names_edit_distance(self, engine):
        assert engine._name_score("contract_no", "", "contract_no") == 1.0


# ============================================
# 通道2 comment：注释缺失 → None 且权重重分配
# ============================================
class TestCommentChannelAndFusion:
    def test_comment_missing_returns_none(self, engine):
        async def _go():
            evidence = await engine._score_candidate(
                spec={"field": "逾期天数", "caliber_text": "逾期天数", "data_type": "INT"},
                cand={"table": "loan_contract", "column": "overdue_days",
                      "data_type": "INTEGER", "comment": ""},  # 无注释
                history_assets=[], profiles={
                    ("loan_contract", "overdue_days"): {"null_rate": 0.1, "distinct_count": 5}},
                pack_id="G11", target_field="逾期天数", caliber_text="逾期天数",
                expected_type="INT", expected_domain=None)
            return evidence
        ev = run(_go())
        assert ev["comment"] is None
        assert ev["history"] is None

    def test_weight_redistribution(self, engine):
        # comment/semantic/history 缺失时，融合分 = 可用通道加权平均（权重重分配）
        evidence = {"name": 0.8, "comment": None, "profile": 0.6,
                    "semantic": None, "history": None}
        fused = engine._fuse(evidence)
        expected = (0.2 * 0.8 + 0.3 * 0.6) / (0.2 + 0.3)
        assert fused == pytest.approx(expected)

    def test_weights_configurable(self):
        e = MappingEngine(embed_fn=fake_embed,
                          weights={"name": 1.0, "comment": 0, "profile": 0,
                                   "semantic": 0, "history": 0})
        assert e._fuse({"name": 0.7, "comment": 0.9, "profile": 0.9,
                        "semantic": 0.9, "history": None}) == pytest.approx(0.7)

    def test_comment_present_uses_cosine(self, engine):
        async def _go():
            return await engine._cosine("逾期天数", "逾期天数")
        assert run(_go()) == pytest.approx(1.0)


# ============================================
# 通道3 profile：类型兼容 + 值域/枚举匹配
# ============================================
class TestProfileChannel:
    def test_type_compatible_and_domain_match(self, engine):
        cand = {"table": "loan_contract", "column": "five_classify", "data_type": "TEXT"}
        profile = {"null_rate": 0.0, "distinct_count": 3,
                   "enum_values": ["1", "2", "3"], "sample_values": ["1", "2", "3"]}
        score = engine._profile_score(cand, profile, "VARCHAR(2)", ["1", "2", "3"])
        assert score == 1.0  # 0.5 类型 + 0.5 枚举全覆盖

    def test_type_incompatible(self, engine):
        cand = {"table": "loan_contract", "column": "repay_date", "data_type": "DATE"}
        score = engine._profile_score(cand, {"sample_values": []}, "DECIMAL(18,2)", None)
        assert score == pytest.approx(0.25)  # 类型不兼容 0 + 中性值域 0.25

    def test_domain_partial_overlap(self, engine):
        cand = {"table": "t", "column": "c", "data_type": "TEXT"}
        profile = {"enum_values": ["1", "9"], "sample_values": ["1", "9"]}
        score = engine._profile_score(cand, profile, "TEXT", ["1", "2"])
        assert score == pytest.approx(0.5 + 0.25)  # 枚举命中一半


# ============================================
# 通道5 history：命中 1.0 / 未命中 None；infer_mappings 端到端
# ============================================
class TestHistoryAndInfer:
    def test_infer_high_confidence_with_comment(self, engine):
        """注释齐全 + 名称命中 → 高置信 ai_inferred"""
        pack = _pack([{"field": "逾期天数", "data_type": "INT",
                       "caliber_text": "逾期天数", "required": True}])
        schemas = _schema([
            {"column_name": "overdue_days", "data_type": "INTEGER",
             "column_comment": "逾期天数"},
            {"column_name": "product_code", "data_type": "TEXT",
             "column_comment": "产品代码"},
        ])
        profiles = {
            ("loan_contract", "overdue_days"): {"null_rate": 0.0, "distinct_count": 4,
                                                "sample_values": [0, 30, 92, 95]},
            ("loan_contract", "product_code"): {"null_rate": 0.0, "distinct_count": 3,
                                                "sample_values": ["P001", "P001-G", "P002"]},
        }
        mappings = run(engine.infer_mappings(pack, schemas, task_id="t1",
                                             history_assets=[], profiles=profiles))
        assert len(mappings) == 1
        m = mappings[0]
        assert m.source_field == "overdue_days"
        assert m.status == MappingStatus.AI_INFERRED
        assert m.confidence >= MIN_CONFIDENCE  # 假 embedding 语义分有稀释，不苛求 0.85
        assert m.evidence["comment"] is not None

    def test_high_confidence_when_semantic_also_hits(self, engine):
        """名称/注释/语义全命中时 → ≥0.85 高置信（stub 余弦模拟真实 BGE 效果）"""
        async def perfect_cosine(left, right):
            return 1.0
        engine._cosine = perfect_cosine  # 仅本用例替换语义计算
        pack = _pack([{"field": "逾期天数", "data_type": "INT",
                       "caliber_text": "逾期天数", "required": True}])
        schemas = _schema([
            {"column_name": "overdue_days", "data_type": "INTEGER",
             "column_comment": "逾期天数"},
        ])
        profiles = {("loan_contract", "overdue_days"): {
            "null_rate": 0.0, "distinct_count": 4, "sample_values": [0, 30, 92, 95]}}
        mappings = run(engine.infer_mappings(pack, schemas, history_assets=[],
                                             profiles=profiles))
        m = mappings[0]
        assert m.confidence >= HIGH_CONFIDENCE
        assert m.status == MappingStatus.AI_INFERRED
        assert m.evidence["level"] == "high"

    def test_history_hit_promotes_score(self, engine):
        """历史资产命中：该候选 history=1.0 并参与融合，提升胜出概率"""
        pack = _pack([{"field": "五级分类", "data_type": "VARCHAR",
                       "caliber_text": "贷款五级分类", "expected_domain": ["1", "2", "3", "4", "5"]}])
        schemas = _schema([
            {"column_name": "five_classify", "data_type": "TEXT", "column_comment": ""},
            {"column_name": "loan_status", "data_type": "TEXT", "column_comment": ""},
        ])
        profiles = {
            ("loan_contract", "five_classify"): {"enum_values": ["1", "2", "3"],
                                                 "sample_values": ["1"], "distinct_count": 3},
            ("loan_contract", "loan_status"): {"enum_values": ["01", "02"],
                                               "sample_values": ["01"], "distinct_count": 2},
        }
        history = [{"report_pack_id": "G11", "target_field": "五级分类",
                    "source_table": "loan_contract", "source_field": "five_classify",
                    "transform_rule": "DIRECT"}]
        mappings = run(engine.infer_mappings(pack, schemas, history_assets=history,
                                             profiles=profiles))
        m = mappings[0]
        assert m.source_field == "five_classify"
        assert m.evidence["history"] == 1.0

    def test_unmapped_when_no_candidate_matches(self, engine):
        """完全无关的候选 → 低置信 unmapped 且源字段为空"""
        pack = _pack([{"field": "国债余额", "data_type": "DECIMAL",
                       "caliber_text": "持有的国债投资余额"}])
        schemas = _schema([
            {"column_name": "org_no", "data_type": "TEXT", "column_comment": "机构号"},
        ])
        profiles = {("loan_contract", "org_no"): {"sample_values": ["1001"],
                                                  "distinct_count": 2}}
        mappings = run(engine.infer_mappings(pack, schemas, history_assets=[],
                                             profiles=profiles))
        m = mappings[0]
        assert m.confidence < MIN_CONFIDENCE
        assert m.status == MappingStatus.UNMAPPED
        assert m.source_table is None and m.source_field is None

    def test_history_loaded_from_platform_db(self, engine):
        """history_assets=None 时从平台库 mapping_assets 查询（真实库路径）"""
        async def _go():
            from backend.database import platform_engine, PlatformSessionLocal, Base
            async with platform_engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            async with PlatformSessionLocal() as s:
                s.add(MappingAsset(id="a1", report_pack_id="G11", target_field="五级分类",
                                   source_table="loan_contract", source_field="five_classify",
                                   transform_rule="DIRECT", use_count=1))
                await s.commit()
            return await MappingEngine._load_history("G11")
        assets = run(_go())
        assert len(assets) == 1
        assert assets[0]["source_field"] == "five_classify"


# ============================================
# 分级边界
# ============================================
class TestGrading:
    def test_boundaries(self, engine):
        assert engine._grade(0.85) == (MappingStatus.AI_INFERRED, "high")
        assert engine._grade(0.849) == (MappingStatus.AI_INFERRED, "medium")
        assert engine._grade(0.5) == (MappingStatus.AI_INFERRED, "medium")
        assert engine._grade(0.499) == (MappingStatus.UNMAPPED, "low")
        assert engine._grade(0.0) == (MappingStatus.UNMAPPED, "low")


# ============================================
# 画像服务：演示数据集上的统计正确性
# ============================================
@pytest.fixture()
def profiling(tmp_path, monkeypatch):
    """独立临时演示库，monkeypatch 掉共享单例，不影响其他测试"""
    from backend.mcp.demo_dataset import DemoDataset
    import backend.mcp.demo_dataset as dd_mod
    ds = DemoDataset(str(tmp_path / "biz.db"))
    ds.ensure_seeded()
    monkeypatch.setattr(dd_mod, "demo_dataset", ds)
    from backend.mcp.database_mcp import DatabaseMCPService
    return ProfilingService(DatabaseMCPService({"db_type": "sqlite_demo"}))


class TestProfiling:
    def test_numeric_column(self, profiling):
        p = run(profiling.profile_column("loan_contract", "loan_amount"))
        assert p["total_rows"] == 12
        assert p["null_rate"] == 0.0
        assert p["distinct_count"] == 12
        assert p["min_value"] == 300000
        assert p["max_value"] == 2000000
        assert p["format_pattern"] == "金额"
        assert p["enum_values"] is None  # 高基数不出枚举
        assert len(p["sample_values"]) <= 10

    def test_null_rate_and_enum(self, profiling):
        p = run(profiling.profile_column("loan_contract", "five_classify"))
        # 12 行中 five_classify 全非空，枚举 {1,2,3}
        assert p["null_rate"] == 0.0
        assert p["distinct_count"] == 3
        assert set(p["enum_values"]) == {"1", "2", "3"}

    def test_date_format(self, profiling):
        p = run(profiling.profile_column("loan_contract", "repay_date"))
        assert p["format_pattern"] == "日期"

    def test_cache_hit(self, profiling):
        p1 = run(profiling.profile_column("loan_contract", "loan_amount"))
        p2 = run(profiling.profile_column("loan_contract", "loan_amount"))
        assert p1 is p2  # 同对象即缓存命中

    def test_bad_identifier_rejected(self, profiling):
        p = run(profiling.profile_column("loan_contract; DROP TABLE x", "c1"))
        assert "error" in p

    def test_summary_text(self):
        text = profile_summary_text({"null_rate": 0.1, "format_pattern": "金额",
                                     "enum_values": None,
                                     "min_value": 1, "max_value": 100})
        assert "金额" in text and "值域" in text
        assert profile_summary_text({"error": "x"}) == ""


# ============================================
# 模型契约：唯一约束与状态枚举
# ============================================
class TestModelContract:
    def test_unique_constraints(self):
        cols = {c.name for c in FieldMapping.__table__.columns}
        assert {"task_id", "report_pack_id", "target_field", "source_table",
                "source_field", "transform_rule", "confidence", "evidence",
                "status", "confirmed_by", "confirmed_at"} <= cols
        uq = [c for c in FieldMapping.__table__.constraints
              if c.__class__.__name__ == "UniqueConstraint"]
        assert any({c.name for c in u.columns} == {"task_id", "target_field"} for u in uq)

        uq2 = [c for c in MappingAsset.__table__.constraints
               if c.__class__.__name__ == "UniqueConstraint"]
        assert any({c.name for c in u.columns} ==
                   {"report_pack_id", "target_field", "source_table", "source_field"}
                   for u in uq2)

    def test_status_enum(self):
        assert MappingStatus.FINAL_STATES == {"confirmed", "modified", "needs_etl"}
