"""
向量库服务（Demo 务实版）
每个租户独立的持久化向量索引，存储于 data/tenants/{tenant_id}/vectors/chunks.json。

实现要点:
- 文档按标题/段落语义切片（chunk_size≈512, overlap≈50）
- Embedding 为可替换点：当前使用本地 hash 伪向量（离线可用），
  配置 embedding_provider=remote 后可切换为 AI 后端真实 embedding
- 检索打分：Jaccard + 中文子串命中 + 标题加成（M1 修复版打分机制的片段级升级）
- 租户隔离：按目录物理隔离，各租户独立 chunks.json
"""

import os
import json
import time
import hashlib
from typing import Dict, Any, List, Optional
from backend.config import settings


class VectorService:
    """租户级向量索引服务"""

    def __init__(self, tenant_id: str):
        self.tenant_id = tenant_id
        self.base_dir = os.path.join(settings.upload_dir, tenant_id)
        self.vectors_dir = os.path.join(self.base_dir, "vectors")
        os.makedirs(self.vectors_dir, exist_ok=True)

    # ============================================
    # 切片
    # ============================================
    def chunk_text(self, content: str, chunk_size: int = 512, overlap: int = 50) -> List[str]:
        """按标题/段落语义切片
        先按 Markdown 标题与空行切段，再按 chunk_size 打包，段间保留 overlap 重叠"""
        # 按标题和空行拆分为语义段落
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

        # 打包为 ~chunk_size 的切片，重叠 overlap 字符
        chunks: List[str] = []
        buf = ""
        for para in paragraphs:
            if len(buf) + len(para) > chunk_size and buf:
                chunks.append(buf.strip())
                buf = buf[-overlap:] + "\n" + para if overlap else para
            else:
                buf = (buf + "\n" + para) if buf else para
            # 单段超长时硬切
            while len(buf) > chunk_size * 2:
                chunks.append(buf[:chunk_size].strip())
                buf = buf[chunk_size - overlap:]
        if buf.strip():
            chunks.append(buf.strip())
        return chunks

    # ============================================
    # 向量化（可替换点）
    # ============================================
    def embed(self, text: str) -> List[float]:
        """文本向量化
        Demo 使用确定性 hash 伪向量（离线可复现）；
        接入真实 embedding 服务时替换本方法即可，索引与检索结构不变"""
        provider = getattr(settings, "embedding_provider", "hash")
        if provider == "remote":
            # 替换点：调用 AI 后端 embeddings 接口
            raise NotImplementedError("真实 embedding 服务未配置，请使用 hash 模式")
        digest = hashlib.md5(text.encode("utf-8")).digest()
        base = [b / 255.0 for b in digest]
        dim = settings.vector_dimension
        return (base * (dim // len(base) + 1))[:dim]

    # ============================================
    # 索引读写
    # ============================================
    def _chunks_path(self) -> str:
        return os.path.join(self.vectors_dir, "chunks.json")

    def load_chunks(self) -> List[Dict[str, Any]]:
        """加载全部切片索引"""
        path = self._chunks_path()
        if not os.path.exists(path):
            return []
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def save_chunks(self, chunks: List[Dict[str, Any]]):
        """持久化全部切片索引"""
        with open(self._chunks_path(), "w", encoding="utf-8") as f:
            json.dump(chunks, f, ensure_ascii=False)

    async def index_document(self, doc_id: str, content: str,
                             doc_type: str, title: str) -> Dict[str, Any]:
        """索引单个文档：切片 → 向量化 → 写入索引（同 doc_id 先清后写，幂等）"""
        start = time.time()
        chunks = self.load_chunks()
        chunks = [c for c in chunks if c["doc_id"] != doc_id]

        texts = self.chunk_text(content)
        for i, text in enumerate(texts):
            chunks.append({
                "chunk_id": f"{doc_id}_c{i}",
                "doc_id": doc_id,
                "doc_type": doc_type,
                "doc_title": title,
                "chunk_index": i,
                "content": text,
                "vector": self.embed(text)
            })

        self.save_chunks(chunks)
        return {
            "doc_id": doc_id,
            "chunk_count": len(texts),
            "duration_ms": int((time.time() - start) * 1000)
        }

    async def remove_document(self, doc_id: str) -> int:
        """从索引中移除文档，返回移除切片数"""
        chunks = self.load_chunks()
        before = len(chunks)
        chunks = [c for c in chunks if c["doc_id"] != doc_id]
        self.save_chunks(chunks)
        return before - len(chunks)

    async def rebuild_all(self, documents: List[Dict[str, Any]]) -> Dict[str, Any]:
        """全量重建索引"""
        self.save_chunks([])
        total_chunks = 0
        for doc in documents:
            r = await self.index_document(doc["doc_id"], doc["content"],
                                          doc["doc_type"], doc["title"])
            total_chunks += r["chunk_count"]
        return {"rebuilt_docs": len(documents), "total_chunks": total_chunks}

    # ============================================
    # 检索
    # ============================================
    def retrieve(self, query: str, doc_type: Optional[str] = None,
                 top_k: int = 5, active_doc_ids: Optional[set] = None) -> Dict[str, Any]:
        """切片级检索：返回 Top-K 片段（排名/相关度/文档名/匹配片段/耗时）"""
        start = time.time()
        chunks = self.load_chunks()

        scored = []
        for chunk in chunks:
            if doc_type and chunk["doc_type"] != doc_type:
                continue
            if active_doc_ids is not None and chunk["doc_id"] not in active_doc_ids:
                continue
            score = self._relevance(query, chunk["content"], chunk["doc_title"])
            if score > 0.15:  # 召回阈值
                scored.append({
                    "doc_id": chunk["doc_id"],
                    "doc_type": chunk["doc_type"],
                    "doc_title": chunk["doc_title"],
                    "content": chunk["content"][:300] + ("..." if len(chunk["content"]) > 300 else ""),
                    "relevance_score": round(score, 2),
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
        """相关度打分：中文 bigram 子串命中 + 英文/数字词命中 + 标题加成
        中文查询通常无空格分词，按连续 CJK 字符二元组切分后做子串匹配"""
        terms = self._tokenize(query)
        if not terms:
            return 0.0

        content_lower = content.lower()
        hits = sum(1 for t in terms if t in content_lower)
        base_score = hits / len(terms)

        # 标题命中加成
        title_lower = title.lower()
        title_hits = sum(1 for t in terms if t in title_lower)
        title_boost = 0.15 * (title_hits / len(terms))

        return base_score + title_boost

    @staticmethod
    def _tokenize(text: str) -> set:
        """查询分词：英文/数字按词，中文按 bigram"""
        import re
        terms = set()
        # 英文与数字词
        for w in re.findall(r"[a-zA-Z0-9_]+", text.lower()):
            if len(w) >= 2:
                terms.add(w)
        # 连续中文字符段切 bigram
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
    def stats(self) -> Dict[str, Any]:
        """向量库统计"""
        chunks = self.load_chunks()
        doc_ids = {c["doc_id"] for c in chunks}
        return {
            "chunk_count": len(chunks),
            "vector_count": len(chunks),
            "indexed_docs": len(doc_ids),
            "vector_dimension": settings.vector_dimension,
            "storage_dir": self.vectors_dir
        }
