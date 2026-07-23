"""
Embedding 服务（L2-D8 真实向量化）
provider 配置真正生效（settings.embedding_provider）：
  - local  ：sentence-transformers 本地语义模型（默认 BAAI/bge-small-zh-v1.5，512 维）；
             模型加载失败自动降级 tfidf 并记日志（离线兜底）
  - remote ：OpenAI 兼容 embedding 端点（/embeddings），用于生产替换
  - tfidf  ：sklearn HashingVectorizer 字符 n-gram TF-IDF（无模型依赖的确定性兜底，
             有真实词项统计信息，远优于原 md5 伪向量）

性能约定：模型推理/HTTP 调用均为重活，embed_* 一律异步（线程池/异步 HTTP），不阻塞事件循环。
"""

import asyncio
import os
import threading
from typing import List, Optional

import numpy as np

# torch 多线程推理与 pytest-asyncio 多事件循环/线程池混用会引发原生崩溃，
# 全局限制为单线程；tokenizers 同理关闭进程内并行。
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
try:
    import torch

    torch.set_num_threads(1)
except ImportError:  # 无 torch 环境（tfidf 兜底）下静默跳过
    pass

# 进程级推理锁：串行化本地模型的 encode 调用（见 _embed_local 注释）
_INFER_LOCK = threading.Lock()

from backend.config import settings
from backend.utils.logging import get_logger

logger = get_logger(__name__)

# 兜底 TF-IDF 维度（HashingVectorizer 无需拟合，确定性输出）
TFIDF_DIM = 1024


class EmbeddingService:
    """全局 embedding 服务（单例）"""

    def __init__(self):
        self.provider = getattr(settings, "embedding_provider", "local")
        self.model_name = getattr(settings, "embedding_model", "BAAI/bge-small-zh-v1.5")
        self._model = None           # sentence-transformers 模型（懒加载）
        self._tfidf = None           # HashingVectorizer（懒加载）
        self._dim: Optional[int] = None
        self._load_lock = asyncio.Lock()

    # ============================================
    # 维度
    # ============================================
    @property
    def dimension(self) -> int:
        if self.provider == "local":
            # BGE-small-zh-v1.5 为 512 维；模型已加载时以实际为准
            if self._model is not None:
                # 新版本方法名为 get_embedding_dimension（旧名已 deprecated）
                getter = getattr(self._model, "get_embedding_dimension", None) \
                    or self._model.get_sentence_embedding_dimension
                return int(getter())
            return settings.vector_dimension
        if self.provider == "tfidf":
            return TFIDF_DIM
        # remote 维度由端点决定，配置兜底
        return settings.vector_dimension

    # ============================================
    # 向量化
    # ============================================
    async def embed_texts(self, texts: List[str]) -> List[List[float]]:
        """批量向量化（异步，不阻塞事件循环）"""
        if not texts:
            return []
        if self.provider == "remote":
            return await self._embed_remote(texts)
        if self.provider == "tfidf":
            return await self._embed_tfidf(texts)
        # local：失败自动降级 tfidf
        try:
            return await self._embed_local(texts)
        except Exception as e:
            logger.warning("本地 embedding 模型不可用，降级 tfidf: %s", e)
            self.provider = "tfidf"
            return await self._embed_tfidf(texts)

    async def embed(self, text: str) -> List[float]:
        """单条向量化"""
        return (await self.embed_texts([text]))[0]

    # ============================================
    # 各 provider 实现
    # ============================================
    async def _embed_local(self, texts: List[str]) -> List[List[float]]:
        """sentence-transformers 本地模型（线程池推理）"""
        async with self._load_lock:
            if self._model is None:
                from sentence_transformers import SentenceTransformer
                logger.info("加载本地 embedding 模型: %s", self.model_name)
                self._model = await asyncio.to_thread(SentenceTransformer, self.model_name)

        def _encode() -> "np.ndarray":
            # torch 原生层在多线程并发推理时不安全（pytest 多事件循环各自起
            # 执行器线程会触发 segfault），进程级锁串行化；inference_mode
            # 进一步减少 autograd 侧线程状态。
            with _INFER_LOCK, torch.inference_mode():
                return self._model.encode(
                    texts,
                    normalize_embeddings=True,  # 归一化后点积即余弦相似度
                    show_progress_bar=False,
                )

        vectors = await asyncio.to_thread(_encode)
        return [v.tolist() for v in np.asarray(vectors)]

    async def _embed_tfidf(self, texts: List[str]) -> List[List[float]]:
        """字符 n-gram TF-IDF 兜底（无模型依赖，含词项统计信息）"""
        if self._tfidf is None:
            from sklearn.feature_extraction.text import HashingVectorizer
            # 中文按字符 bigram/trigram 提取，与检索侧分词习惯一致
            self._tfidf = HashingVectorizer(
                n_features=TFIDF_DIM, analyzer="char_wb", ngram_range=(2, 4),
                norm="l2", alternate_sign=False,
            )
        matrix = await asyncio.to_thread(self._tfidf.transform, texts)
        return matrix.toarray().tolist()

    async def _embed_remote(self, texts: List[str]) -> List[List[float]]:
        """OpenAI 兼容 embedding 端点"""
        import httpx
        base_url = getattr(settings, "embedding_remote_base_url", "") or settings.ai_base_url
        api_key = getattr(settings, "embedding_remote_api_key", "") or settings.ai_api_key
        model = getattr(settings, "embedding_remote_model", "") or self.model_name
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{base_url.rstrip('/')}/embeddings",
                headers={"Authorization": f"Bearer {api_key}"},
                json={"model": model, "input": texts},
            )
            resp.raise_for_status()
            data = resp.json()
        vectors = [item["embedding"] for item in sorted(data["data"], key=lambda x: x["index"])]
        # L2 归一化，与 local 保持同一度量
        arr = np.asarray(vectors, dtype=np.float32)
        norms = np.linalg.norm(arr, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return (arr / norms).tolist()


# 全局单例
embedding_service = EmbeddingService()
