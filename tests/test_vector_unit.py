"""
向量库服务纯函数单测（L2-Day10 补齐）
覆盖 chunk_text 语义切片（标题切段/打包/重叠/超长段硬切）与
_tokenize / _relevance 文本通道打分。不加载 embedding 模型、不触库。

运行方式: python -m pytest tests/test_vector_unit.py -v
"""

import os
import tempfile

# 在导入 backend 前切换到临时目录（VectorService 初始化会建租户目录）
_tmpdir = tempfile.mkdtemp(prefix="rrp_test_vunit_")
os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{_tmpdir}/test_platform.db")
os.environ.setdefault("UPLOAD_DIR", f"{_tmpdir}/tenants")
os.environ.setdefault("DEMO_DB_PATH", f"{_tmpdir}/demo_biz.db")
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-pytest")
os.environ.setdefault("DEBUG", "false")

import pytest

from backend.services.vector_service import VectorService


@pytest.fixture(scope="module")
def vs():
    # 仅用于调用纯函数，不做任何 IO
    return VectorService("T999")


# ============================================
# chunk_text 切片
# ============================================

class TestChunkText:
    def test_empty_content(self, vs):
        assert vs.chunk_text("") == []
        assert vs.chunk_text("\n\n  \n") == []

    def test_short_single_chunk(self, vs):
        chunks = vs.chunk_text("# 标题\n一小段制度内容。")
        assert len(chunks) == 1
        assert "标题" in chunks[0] and "一小段制度内容。" in chunks[0]

    def test_heading_splits_paragraphs(self, vs):
        """Markdown 标题开启新段；短文档仍打包为一个切片，但段落结构参与切分"""
        content = "# 第一章 总则\n内容一。\n# 第二章 细则\n内容二。"
        chunks = vs.chunk_text(content, chunk_size=20)
        assert len(chunks) >= 2
        joined = "\n".join(chunks)
        assert "第一章" in joined and "第二章" in joined

    def test_packing_respects_chunk_size(self, vs):
        """多个中段按 chunk_size 打包，不产生远超上限的切片"""
        paras = "\n\n".join(f"第{i}段内容，" + "字" * 60 for i in range(10))
        chunks = vs.chunk_text(paras, chunk_size=100, overlap=10)
        assert len(chunks) >= 3
        # 打包路径下单片不应超过 chunk_size + 单段长度太多（硬切路径保证 <= 2x）
        assert all(len(c) <= 200 for c in chunks)

    def test_overlap_continuity(self, vs):
        """相邻切片存在重叠内容（检索不断章）"""
        paras = "\n\n".join(f"段落{i}，" + "数" * 50 for i in range(6))
        chunks = vs.chunk_text(paras, chunk_size=80, overlap=20)
        assert len(chunks) >= 2
        # 后一切片开头应包含前一切片末尾的若干字符
        assert chunks[0][-10:-5] in chunks[1]

    def test_very_long_paragraph_hard_split(self, vs):
        """单段远超 chunk_size 时硬切成固定长度片"""
        long_para = "长" * 1000
        chunks = vs.chunk_text(long_para, chunk_size=100, overlap=10)
        assert len(chunks) >= 5
        assert all(len(c) <= 200 for c in chunks)
        # 内容总量基本保留（硬切不丢字）
        assert sum(len(c) for c in chunks) >= 1000

    def test_no_overlap_mode(self, vs):
        """overlap=0 时不做重叠拼接"""
        paras = "\n\n".join(f"段{i}" + "字" * 60 for i in range(6))
        chunks = vs.chunk_text(paras, chunk_size=100, overlap=0)
        assert len(chunks) >= 2


# ============================================
# _tokenize 查询分词
# ============================================

class TestTokenize:
    def test_chinese_bigram(self):
        terms = VectorService._tokenize("逾期本金")
        assert terms == {"逾期", "期本", "本金"}

    def test_single_chinese_char(self):
        assert "贷" in VectorService._tokenize("贷")

    def test_english_words_min_length(self):
        terms = VectorService._tokenize("EAST lpr a")
        assert "east" in terms and "lpr" in terms
        assert "a" not in terms  # 单字符英文词丢弃

    def test_mixed(self):
        terms = VectorService._tokenize("EAST制度90天")
        assert "east" in terms and "90" in terms
        assert "制度" in terms

    def test_empty(self):
        assert VectorService._tokenize("!@#$") == set()


# ============================================
# _relevance 文本通道打分
# ============================================

class TestRelevance:
    def test_full_hit(self, vs):
        score = vs._relevance("逾期本金", "逾期本金按90天分界填报", "其他标题")
        assert score == 1.0

    def test_partial_hit(self, vs):
        score = vs._relevance("逾期本金", "本制度仅含本金二字", "无")
        assert 0 < score < 1.0

    def test_no_hit(self, vs):
        assert vs._relevance("逾期本金", "完全无关的内容", "无关") == 0.0

    def test_title_boost(self, vs):
        """标题命中加成 0.15 权重"""
        content_only = vs._relevance("逾期本金", "逾期本金", "无")
        with_title = vs._relevance("逾期本金", "逾期本金", "逾期本金口径")
        assert with_title > content_only
        assert abs((with_title - content_only) - 0.15) < 1e-6

    def test_empty_query_terms(self, vs):
        assert vs._relevance("!@#", "任何内容", "标题") == 0.0

    def test_case_insensitive_english(self, vs):
        assert vs._relevance("east 口径", "EAST 口径说明", "") > 0
