"""
向量库服务（L2-D8 真实向量版）
每个租户独立的 SQLite 向量索引：data/tenants/{tenant_id}/vectors/vectors.db。

实现要点:
- Embedding：embedding_service（local 语义模型 / remote 端点 / tfidf 兜底），
  全部异步，不阻塞事件循环
- 存储：SQLite 单表 chunks，索引写入单事务原子提交（替代原 JSON 全量读改写，
  并发索引不再丢数据；SQLite 单写者天然串行化写冲突）
- 检索：双通道融合——向量余弦相似度（embedding 已 L2 归一化，点积即余弦）
  + bigram 文本打分，按权重融合（默认 0.7/0.3，可配置）
- 租户隔离：按目录物理隔离，各租户独立 vectors.db
- 旧版 chunks.json 索引首次访问时自动一次性迁移
"""

import os
import json
import time
import asyncio
from typing import Dict, Any, List, Optional

import aiosqlite
import numpy as np

from backend.config import settings
from backend.services.embedding_service import embedding_service


class VectorService:
    """租户级向量索引服务"""

    def __init__(self, tenant_id: str):
        self.tenant_id = tenant_id
        self.base_dir = os.path.join(settings.upload_dir, tenant_id)
        self.vectors_dir = os.path.join(self.base_dir, "vectors")
        os.makedirs(self.vectors_dir, exist_ok=True)
        self._db_path = os.path.join(self.vectors_dir, "vectors.db")
        self._initialized = False
        self._init_lock = asyncio.Lock()

    # ============================================
    # 存储初始化与旧版迁移
    # ============================================
    async def _ensure_db(self):
        """建表 + 旧版 chunks.json 一次性迁移（幂等）"""
        if self._initialized:
            return
        async with self._init_lock:
            if self._initialized:
                return
            async with aiosqlite.connect(self._db_path) as db:
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS chunks (
                        chunk_id TEXT PRIMARY KEY,
                        doc_id TEXT NOT NULL,
                        doc_type TEXT,
                        doc_title TEXT,
                        chunk_index INTEGER,
                        content TEXT,
                        vector TEXT
                    )
                """)
                await db.execute("CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(doc_id)")
                await db.commit()

            # 旧版 JSON 索引迁移（向量已失效，仅迁移文本与元数据，向量重建）
            legacy = os.path.join(self.vectors_dir, "chunks.json")
            if os.path.exists(legacy):
                try:
                    with open(legacy, "r", encoding="utf-8") as f:
                        old = json.load(f)
                    if old:
                        texts = [c["content"] for c in old]
                        vectors = await embedding_service.embed_texts(texts)
                        async with aiosqlite.connect(self._db_path) as db:
                            for c, v in zip(old, vectors):
                                await db.execute(
                                    "INSERT OR REPLACE INTO chunks VALUES (?,?,?,?,?,?,?)",
                                    (c["chunk_id"], c["doc_id"], c.get("doc_type", ""),
                                     c.get("doc_title", ""), c.get("chunk_index", 0),
                                     c["content"], json.dumps(v)))
                            await db.commit()
                    os.rename(legacy, legacy + ".bak")
                except Exception:
                    # 迁移失败不阻塞：保留原文件，由 reindex 重建
                    pass
            self._initialized = True

    # ============================================
    # 切片
    # ============================================
    def chunk_text(self, content: str, chunk_size: int = 512, overlap: int = 50) -> List[str]:
        """按标题/段落语义切片
        先按 Markdown 标题与空行切段，再按 chunk_size 打包，段间保留 overlap 重叠"""
        paragraphs: List[str] = []
        current = ""
        for line in content.splitlines():
            if line.strip().startswith("#") or (not line.strip() and current.strip()):
                if current.strip():
                    paragraphs.append(current.strip())
                current = line + "\n"
            else:
                current += line + "\n"
        if current.strip():
            paragraphs.append(current.strip())

        chunks: List[str] = []
        buf = ""
        for para in paragraphs:
            if len(buf) + len(para) > chunk_size and buf:
                chunks.append(buf.strip())
                buf = buf[-overlap:] + "\n" + para if overlap else para
            else:
                buf = (buf + "\n" + para) if buf else para
            while len(buf) > chunk_size * 2:
                chunks.append(buf[:chunk_size].strip())
                buf = buf[chunk_size - overlap:]
        if buf.strip():
            chunks.append(buf.strip())
        return chunks

    # ============================================
    # 索引读写（单事务原子提交）
    # ============================================
    async def load_chunks(self) -> List[Dict[str, Any]]:
        """加载全部切片索引"""
        await self._ensure_db()
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT chunk_id, doc_id, doc_type, doc_title, chunk_index, content, vector FROM chunks")
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def index_document(self, doc_id: str, content: str,
                             doc_type: str, title: str) -> Dict[str, Any]:
        """索引单个文档：切片 → 批量向量化 → 单事务写入（同 doc_id 先清后写，幂等且原子）"""
        start = time.time()
        await self._ensure_db()

        texts = self.chunk_text(content)
        vectors = await embedding_service.embed_texts(texts)

        async with aiosqlite.connect(self._db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            await db.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))
            for i, (text, vector) in enumerate(zip(texts, vectors)):
                await db.execute(
                    "INSERT INTO chunks VALUES (?,?,?,?,?,?,?)",
                    (f"{doc_id}_c{i}", doc_id, doc_type, title, i, text, json.dumps(vector)))
            await db.commit()

        return {
            "doc_id": doc_id,
            "chunk_count": len(texts),
            "duration_ms": int((time.time() - start) * 1000)
        }

    async def remove_document(self, doc_id: str) -> int:
        """从索引中移除文档，返回移除切片数"""
        await self._ensure_db()
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))
            removed = cur.rowcount
            await db.commit()
        return removed

    async def rebuild_all(self, documents: List[Dict[str, Any]]) -> Dict[str, Any]:
        """全量重建索引"""
        await self._ensure_db()
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute("DELETE FROM chunks")
            await db.commit()
        total_chunks = 0
        for doc in documents:
            r = await self.index_document(doc["doc_id"], doc["content"],
                                          doc["doc_type"], doc["title"])
            total_chunks += r["chunk_count"]
        return {"rebuilt_docs": len(documents), "total_chunks": total_chunks}

    # ============================================
    # 双通道融合检索
    # ============================================
    async def retrieve(self, query: str, doc_type: Optional[str] = None,
                       top_k: int = 5, active_doc_ids: Optional[set] = None) -> Dict[str, Any]:
        """切片级检索：向量余弦 + bigram 文本双通道加权融合
        返回 Top-K（排名/向量分/文本分/融合分/文档名/匹配片段/耗时）"""
        start = time.time()
        chunks = await self.load_chunks()

        # 过滤通道
        candidates = []
        for chunk in chunks:
            if doc_type and chunk["doc_type"] != doc_type:
                continue
            if active_doc_ids is not None and chunk["doc_id"] not in active_doc_ids:
                continue
            candidates.append(chunk)

        if not candidates:
            return {"results": [], "total_found": 0, "elapsed_ms": 0}

        # 向量通道：归一化向量点积 = 余弦相似度
        query_vector = np.asarray(await embedding_service.embed(query), dtype=np.float32)
        doc_vectors = np.asarray([json.loads(c["vector"]) for c in candidates], dtype=np.float32)
        # 维度不一致（provider 切换后未重建索引）时跳过向量通道，避免崩溃
        if doc_vectors.ndim == 2 and doc_vectors.shape[1] == query_vector.shape[0]:
            cosine_scores = doc_vectors @ query_vector
        else:
            cosine_scores = np.zeros(len(candidates), dtype=np.float32)

        w_vec = settings.retrieval_vector_weight
        w_text = settings.retrieval_text_weight
        threshold = settings.retrieval_threshold

        scored = []
        for chunk, cos in zip(candidates, cosine_scores):
            vector_score = max(0.0, float(cos))  # 负相似度截断为 0
            text_score = self._relevance(query, chunk["content"], chunk["doc_title"])
            fused = w_vec * vector_score + w_text * min(text_score, 1.0)
            if fused >= threshold:
                scored.append({
                    "doc_id": chunk["doc_id"],
                    "doc_type": chunk["doc_type"],
                    "doc_title": chunk["doc_title"],
                    "content": chunk["content"][:300] + ("..." if len(chunk["content"]) > 300 else ""),
                    "vector_score": round(vector_score, 3),
                    "text_score": round(text_score, 3),
                    "relevance_score": round(fused, 3),
                    "source_file": f"{chunk['doc_title']}.txt",
                    "chunk_index": chunk["chunk_index"]
                })

        scored.sort(key=lambda x: x["relevance_score"], reverse=True)
        return {
            "results": scored[:top_k],
            "total_found": len(scored),
            "elapsed_ms": int((time.time() - start) * 1000)
        }

    def _relevance(self, query: str, content: str, title: str) -> float:
        """文本通道打分：中文 bigram 子串命中 + 英文/数字词命中 + 标题加成"""
        terms = self._tokenize(query)
        if not terms:
            return 0.0

        content_lower = content.lower()
        hits = sum(1 for t in terms if t in content_lower)
        base_score = hits / len(terms)

        title_lower = title.lower()
        title_hits = sum(1 for t in terms if t in title_lower)
        title_boost = 0.15 * (title_hits / len(terms))

        return base_score + title_boost

    @staticmethod
    def _tokenize(text: str) -> set:
        """查询分词：英文/数字按词，中文按 bigram"""
        import re
        terms = set()
        for w in re.findall(r"[a-zA-Z0-9_]+", text.lower()):
            if len(w) >= 2:
                terms.add(w)
        for seg in re.findall(r"[一-鿿]+", text):
            if len(seg) == 1:
                terms.add(seg)
            else:
                for i in range(len(seg) - 1):
                    terms.add(seg[i:i + 2])
        return terms

    # ============================================
    # 统计
    # ============================================
    async def stats(self) -> Dict[str, Any]:
        """向量库统计"""
        await self._ensure_db()
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute("SELECT COUNT(*), COUNT(DISTINCT doc_id) FROM chunks")
            chunk_count, doc_count = (await cur.fetchone())
        return {
            "chunk_count": chunk_count,
            "vector_count": chunk_count,
            "indexed_docs": doc_count,
            "vector_dimension": embedding_service.dimension,
            "embedding_provider": embedding_service.provider,
            "storage_dir": self.vectors_dir
        }
