"""
映射推断引擎（设计方案 §2.3）

对每个目标字段 × 每个候选 (source_table, source_field) 计算五通道证据分：
  1. name     名称相似度：编辑距离 + 拼音首字母（pypinyin 可用时）+ 缩写词典
  2. comment  注释语义：BGE 余弦（注释缺失 → 通道为 None，不参与加权）
  3. profile  数据画像：类型兼容 + 值域/枚举与 expected_domain 匹配
  4. semantic 制度语义：caliber_text ↔ (字段名+注释+画像摘要) 的 BGE 余弦
  5. history  历史映射资产命中：1.0 / None

融合 = 可用通道加权平均（权重可配，默认 name.2/comment.2/profile.3/semantic.2/history.1）
分级：≥0.85 ai_inferred(高置信) / 0.5-0.85 ai_inferred(待确认, evidence.level=medium) / <0.5 unmapped

返回未持久化的 FieldMapping 对象列表，由编排层（范围 C）落库。
"""

import uuid
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional

import numpy as np

from backend.models.field_mapping import FieldMapping, MappingStatus
from backend.services.profiling_service import ProfilingService, profile_summary_text
from backend.utils.logging import get_logger

logger = get_logger(__name__)

# ============================================
# 可配参数
# ============================================
DEFAULT_WEIGHTS = {"name": 0.2, "comment": 0.2, "profile": 0.3, "semantic": 0.2, "history": 0.1}
HIGH_CONFIDENCE = 0.85   # ≥ 此值：高置信
MIN_CONFIDENCE = 0.5     # ≥ 此值：AI 推断待确认；低于则 unmapped

# 缩写词典：源字段常见缩写 → 中文语义（设计方案指定条目 + 演示常用扩展）
ABBREV_DICT = {
    "amt": "金额", "bal": "余额", "no": "编号", "days": "天数", "class": "分类",
    "classify": "分类", "cls": "分类",
    "cust": "客户", "org": "机构", "prod": "产品", "rate": "利率", "date": "日期",
    "status": "状态", "type": "类型", "code": "代码", "id": "标识",
    "principal": "本金", "interest": "利息", "overdue": "逾期", "loan": "贷款",
}

# 类型兼容分组（粗粒度，演示深度）
_NUMERIC_TYPES = {"INT", "INTEGER", "BIGINT", "SMALLINT", "TINYINT", "FLOAT", "DOUBLE",
                  "DECIMAL", "NUMERIC", "REAL", "NUMBER"}
_TEXT_TYPES = {"CHAR", "VARCHAR", "TEXT", "STRING", "NVARCHAR", "CLOB"}
_DATE_TYPES = {"DATE", "DATETIME", "TIMESTAMP", "TIME"}


def _type_group(data_type: str) -> str:
    """把数据库类型归并为 numeric / text / date 三组"""
    t = (data_type or "").upper().split("(")[0].strip()
    if t in _NUMERIC_TYPES:
        return "numeric"
    if t in _DATE_TYPES:
        return "date"
    return "text"


def _getattr(obj: Any, name: str, default=None):
    """同时支持 dict 与 ORM 对象取字段"""
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


class MappingEngine:
    """五通道映射推断引擎"""

    def __init__(self, db_mcp=None, profiling: ProfilingService = None,
                 embed_fn=None, weights: Dict[str, float] = None):
        """
        embed_fn: 文本向量函数（默认复用 vector_service 同款 embedding_service.embed；
                  测试可注入确定性假向量，避免加载模型）
        weights: 五通道权重，缺省 DEFAULT_WEIGHTS
        """
        self.profiling = profiling or ProfilingService(db_mcp)
        self.weights = dict(weights or DEFAULT_WEIGHTS)
        if embed_fn is None:
            from backend.services.embedding_service import embedding_service
            embed_fn = embedding_service.embed
        self._embed_fn = embed_fn
        # 拼音首字母通道：pypinyin 可选依赖，缺失时该子信号降级为 0
        try:
            from pypinyin import lazy_pinyin, Style
            self._pinyin = lambda s: "".join(lazy_pinyin(s, style=Style.FIRST_LETTER))
        except ImportError:
            self._pinyin = None

    # ============================================
    # 主入口
    # ============================================
    async def infer_mappings(self, report_pack: Any, schemas: Dict[str, Any],
                             task_id: str = "",
                             history_assets: Optional[List[Dict[str, Any]]] = None,
                             profiles: Optional[Dict[tuple, Dict[str, Any]]] = None
                             ) -> List[FieldMapping]:
        """
        report_pack: ORM 对象或 dict（含 id / target_schema / source_tables）
        schemas: {table: query_schema 返回结构 或 columns 列表}
        history_assets: 该场景包的历史映射资产列表；None 时从平台库查询
        profiles: 预取画像 {(table, column): profile}；None 时实时走只读通道画像
        """
        pack_id = _getattr(report_pack, "id") or _getattr(report_pack, "report_pack_id", "")
        target_schema = _getattr(report_pack, "target_schema", []) or []
        source_tables = _getattr(report_pack, "source_tables", []) or []

        if history_assets is None:
            history_assets = await self._load_history(pack_id)

        # 候选 (table, column, data_type, comment) 展开
        candidates = self._expand_candidates(schemas, source_tables)

        results: List[FieldMapping] = []
        for spec in target_schema:
            target_field = _getattr(spec, "field", "")
            if not target_field:
                continue
            mapping = await self._infer_one(
                pack_id=pack_id, task_id=task_id, spec=spec,
                candidates=candidates, history_assets=history_assets, profiles=profiles)
            results.append(mapping)
        return results

    # ============================================
    # 单字段推断
    # ============================================
    async def _infer_one(self, pack_id: str, task_id: str, spec: Any,
                         candidates: List[Dict[str, Any]],
                         history_assets: List[Dict[str, Any]],
                         profiles: Optional[Dict[tuple, Dict[str, Any]]]) -> FieldMapping:
        target_field = _getattr(spec, "field")
        caliber_text = _getattr(spec, "caliber_text", "") or ""
        expected_type = _getattr(spec, "data_type", "") or ""
        expected_domain = _getattr(spec, "expected_domain") or None

        best = None  # (fused, candidate, evidence)
        for cand in candidates:
            evidence = await self._score_candidate(
                spec, cand, history_assets, profiles, pack_id,
                target_field, caliber_text, expected_type, expected_domain)
            fused = self._fuse(evidence)
            if best is None or fused > best[0]:
                best = (fused, cand, evidence)

        fused, cand, evidence = best if best else (0.0, None, {})
        status, level = self._grade(fused)
        evidence = {k: (round(v, 4) if v is not None else None) for k, v in evidence.items()}
        evidence["level"] = level

        return FieldMapping(
            id=uuid.uuid4().hex[:32],
            task_id=task_id,
            report_pack_id=pack_id,
            target_field=target_field,
            source_table=cand["table"] if cand and status != MappingStatus.UNMAPPED else None,
            source_field=cand["column"] if cand and status != MappingStatus.UNMAPPED else None,
            transform_rule="DIRECT",
            confidence=round(fused, 4),
            evidence=evidence,
            status=status,
        )

    async def _score_candidate(self, spec: Any, cand: Dict[str, Any],
                               history_assets: List[Dict[str, Any]],
                               profiles: Optional[Dict[tuple, Dict[str, Any]]],
                               pack_id: str,
                               target_field: str, caliber_text: str,
                               expected_type: str, expected_domain) -> Dict[str, Optional[float]]:
        """计算单候选的五通道证据（None 表示通道不可用，不参与加权）"""
        comment = (cand.get("comment") or "").strip()

        # 通道1 名称相似度（纯本地计算）
        name_score = self._name_score(target_field, caliber_text, cand["column"])

        # 通道2 注释语义（注释缺失 → None 不计权重）
        if comment:
            query_text = caliber_text or target_field
            comment_score = await self._cosine(query_text, comment)
        else:
            comment_score = None

        # 通道3 数据画像（类型兼容 + 值域/枚举匹配）
        profile = None
        if profiles is not None:
            profile = profiles.get((cand["table"], cand["column"]))
        if profile is None:
            profile = await self.profiling.profile_column(cand["table"], cand["column"])
        profile_score = self._profile_score(cand, profile, expected_type, expected_domain)

        # 通道4 制度语义：caliber_text ↔ 字段名+注释+画像摘要
        sem_right = " ".join(x for x in [cand["column"], comment,
                                         profile_summary_text(profile)] if x)
        if caliber_text and sem_right:
            semantic_score = await self._cosine(caliber_text, sem_right)
        else:
            semantic_score = None

        # 通道5 历史资产命中
        hit = any(
            a.get("report_pack_id") == pack_id
            and a.get("target_field") == target_field
            and a.get("source_table") == cand["table"]
            and a.get("source_field") == cand["column"]
            for a in history_assets)
        history_score = 1.0 if hit else None

        return {"name": name_score, "comment": comment_score, "profile": profile_score,
                "semantic": semantic_score, "history": history_score}

    # ============================================
    # 通道实现
    # ============================================
    def _name_score(self, target_field: str, caliber_text: str, source_field: str) -> float:
        """名称相似度：编辑距离 / 缩写词典展开命中 / 拼音首字母（可选）三者取优"""
        sf = source_field.lower()
        tf = target_field.lower()

        # 1) 编辑距离（difflib 比率）
        edit = SequenceMatcher(None, sf, tf).ratio()

        # 2) 缩写词典展开：源字段下划线分词，逐词展开后与目标字段/口径文本比对
        tokens = [t for t in sf.replace("-", "_").split("_") if t]
        hits = 0
        haystack = target_field + (caliber_text or "")
        for tok in tokens:
            expanded = ABBREV_DICT.get(tok)
            if expanded and expanded in haystack:
                hits += 1
            elif tok in tf:  # 未缩写词直接命中
                hits += 1
        dict_score = hits / len(tokens) if tokens else 0.0

        # 3) 拼音首字母：目标字段为中文时，比较其首字母串与源字段
        pinyin_score = 0.0
        if self._pinyin and any("一" <= ch <= "鿿" for ch in target_field):
            initials = self._pinyin(target_field).lower()
            compact = sf.replace("_", "")
            if initials:
                pinyin_score = SequenceMatcher(None, compact, initials).ratio()

        return round(max(edit, dict_score, pinyin_score), 4)

    @staticmethod
    def _profile_score(cand: Dict[str, Any], profile: Dict[str, Any],
                       expected_type: str, expected_domain) -> float:
        """画像通道：类型兼容 0.5 + 值域/枚举匹配 0.5"""
        # 类型兼容
        src_group = _type_group(cand.get("data_type", ""))
        tgt_group = _type_group(expected_type)
        if not expected_type:
            type_part = 0.3  # 目标未声明类型：中性偏保守
        elif src_group == tgt_group:
            type_part = 0.5
        elif {src_group, tgt_group} <= {"numeric", "text"}:
            type_part = 0.25  # 数值/文本可转换：部分兼容
        else:
            type_part = 0.0

        # 值域/枚举匹配
        if expected_domain:
            enums = profile.get("enum_values") or []
            expected = {str(v) for v in expected_domain}
            actual = {str(v) for v in enums}
            if actual:
                overlap = len(expected & actual) / max(len(expected), 1)
                domain_part = 0.5 * min(overlap, 1.0)
            else:
                # 高基数字段无法枚举比对，用样例值抽查
                samples = {str(v) for v in (profile.get("sample_values") or [])}
                domain_part = 0.25 if expected & samples else 0.1
        else:
            domain_part = 0.25  # 未声明期望域：中性分

        return round(type_part + domain_part, 4)

    async def _cosine(self, left: str, right: str) -> float:
        """BGE 余弦（向量已 L2 归一化，点积即余弦）；embedding 失败时降级 bigram 文本相似度"""
        try:
            vl = np.asarray(await self._embed_fn(left), dtype=np.float32)
            vr = np.asarray(await self._embed_fn(right), dtype=np.float32)
            if vl.shape != vr.shape:
                raise ValueError("向量维度不一致")
            return max(0.0, min(1.0, float(vl @ vr)))
        except Exception as e:
            logger.warning("embedding 不可用，降级 bigram 文本相似度: %s", e)
            return self._bigram_score(left, right)

    @staticmethod
    def _bigram_score(left: str, right: str) -> float:
        """离线兜底：中文 bigram + 英文词命中率（与 vector_service 文本通道同思路）"""
        import re
        terms = set()
        for w in re.findall(r"[a-zA-Z0-9_]+", left.lower()):
            if len(w) >= 2:
                terms.add(w)
        for seg in re.findall(r"[一-鿿]+", left):
            if len(seg) == 1:
                terms.add(seg)
            else:
                terms.update(seg[i:i + 2] for i in range(len(seg) - 1))
        if not terms:
            return 0.0
        right_lower = right.lower()
        return sum(1 for t in terms if t in right_lower) / len(terms)

    # ============================================
    # 融合与分级
    # ============================================
    def _fuse(self, evidence: Dict[str, Optional[float]]) -> float:
        """可用通道加权平均；某通道 None（注释缺失/未命中历史等）时权重自动重分配"""
        total_w, acc = 0.0, 0.0
        for channel, score in evidence.items():
            if score is None:
                continue
            w = self.weights.get(channel, 0.0)
            acc += w * score
            total_w += w
        return acc / total_w if total_w > 0 else 0.0

    @staticmethod
    def _grade(fused: float):
        """分级：≥0.85 高置信 ai_inferred；0.5-0.85 待确认 ai_inferred(level=medium)；<0.5 unmapped"""
        if fused >= HIGH_CONFIDENCE:
            return MappingStatus.AI_INFERRED, "high"
        if fused >= MIN_CONFIDENCE:
            return MappingStatus.AI_INFERRED, "medium"
        return MappingStatus.UNMAPPED, "low"

    # ============================================
    # 辅助
    # ============================================
    @staticmethod
    def _expand_candidates(schemas: Dict[str, Any], source_tables: List[str]
                           ) -> List[Dict[str, Any]]:
        """把 schemas 展开成候选列表 [{table, column, data_type, comment}]"""
        candidates = []
        tables = source_tables or list(schemas.keys())
        for table in tables:
            schema = schemas.get(table)
            if not schema:
                continue
            cols = schema.get("columns", []) if isinstance(schema, dict) else schema
            for col in cols:
                name = _getattr(col, "column_name") or _getattr(col, "name")
                if not name:
                    continue
                candidates.append({
                    "table": table,
                    "column": name,
                    "data_type": _getattr(col, "data_type", "") or "",
                    "comment": _getattr(col, "column_comment", "") or "",
                })
        return candidates

    @staticmethod
    async def _load_history(pack_id: str) -> List[Dict[str, Any]]:
        """从平台库加载该场景包的历史映射资产（表不存在等异常时降级为空集）"""
        try:
            from sqlalchemy import select
            from backend.database import PlatformSessionLocal
            from backend.models.mapping_asset import MappingAsset
            async with PlatformSessionLocal() as session:
                rows = (await session.execute(
                    select(MappingAsset).where(MappingAsset.report_pack_id == pack_id)
                )).scalars().all()
            return [{
                "report_pack_id": r.report_pack_id,
                "target_field": r.target_field,
                "source_table": r.source_table,
                "source_field": r.source_field,
                "transform_rule": r.transform_rule,
            } for r in rows]
        except Exception as e:
            logger.warning("历史映射资产加载失败（按空集处理）: %s", e)
            return []
