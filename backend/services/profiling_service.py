"""
数据画像服务（设计方案 §2.2）

对候选源表字段做轻量聚合画像：
  {null_rate, distinct_count, sample_values(≤10), min/max(数值), format_pattern, enum_values(低基数)}

实现要点：
- 全部统计走 database_mcp 只读通道（sqlglot AST 白名单 + 超时 + 行限 + 脱敏），
  仅发起聚合/去重采样 SELECT，不做全库扫描（方案 §五 边界）
- 结果按 (table, column) 进程内缓存；重复画像零成本
- 表名/列名先做标识符白名单校验（防注入；sqlglot 白名单只保证语句形状）
- 全部异步，不阻塞事件循环
"""

import re
from typing import Any, Dict, List, Optional

from backend.utils.logging import get_logger

logger = get_logger(__name__)

# 表名/列名标识符白名单（仅字母数字下划线，防注入）
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# 低基数阈值：distinct 数不超过该值时输出枚举值
ENUM_MAX_DISTINCT = 10

# 格式正则（对样例值做多数派判定）
_FORMAT_PATTERNS = {
    "身份证号": re.compile(r"^(\d{15}|\d{17}[\dXx])$"),
    "手机号": re.compile(r"^1[3-9]\d{9}$"),
    "日期": re.compile(r"^(\d{4}[-/]\d{1,2}[-/]\d{1,2}|\d{8})$"),
}
# 金额判定：数值型且含小数或绝对值较大（>=1000）
_AMOUNT_MIN_ABS = 1000

# 样例值多数派阈值
_PATTERN_HIT_RATIO = 0.8


class ProfilingService:
    """字段画像服务（按 (table, column) 缓存）"""

    def __init__(self, db_mcp=None):
        # 默认走 SQLite 演示数据集的只读通道；生产可注入 mysql 配置的 DatabaseMCPService
        if db_mcp is None:
            from backend.mcp.database_mcp import DatabaseMCPService
            db_mcp = DatabaseMCPService({"db_type": "sqlite_demo"})
        self.db_mcp = db_mcp
        self._cache: Dict[tuple, Dict[str, Any]] = {}

    # ============================================
    # 主入口
    # ============================================
    async def profile_column(self, table: str, column: str) -> Dict[str, Any]:
        """画像单个字段，结果缓存；失败时返回带 error 的最小画像（不阻断映射推断）"""
        key = (table, column)
        if key in self._cache:
            return self._cache[key]

        if not (_IDENT_RE.match(table) and _IDENT_RE.match(column)):
            # 非法标识符直接拒绝，不发起 SQL
            profile = {"table": table, "column": column, "error": "非法表名/列名"}
            self._cache[key] = profile
            return profile

        try:
            profile = await self._do_profile(table, column)
        except Exception as e:
            logger.warning("画像失败 %s.%s: %s", table, column, e)
            profile = {"table": table, "column": column, "error": str(e)[:200]}
        self._cache[key] = profile
        return profile

    async def profile_table(self, table: str, columns: List[str]) -> Dict[str, Dict[str, Any]]:
        """批量画像（顺序执行，演示深度足够；缓存命中即返回）"""
        return {c: await self.profile_column(table, c) for c in columns}

    def clear_cache(self):
        self._cache.clear()

    # ============================================
    # 统计实现（只读聚合 SQL）
    # ============================================
    async def _do_profile(self, table: str, column: str) -> Dict[str, Any]:
        # 聚合统计：总数/空值/去重/min/max（一条 SQL 完成）
        agg_sql = (
            f"SELECT COUNT(*) AS total_rows, "
            f"SUM(CASE WHEN {column} IS NULL THEN 1 ELSE 0 END) AS null_rows, "
            f"COUNT(DISTINCT {column}) AS distinct_count, "
            f"MIN({column}) AS min_value, MAX({column}) AS max_value "
            f"FROM {table}"
        )
        agg = await self.db_mcp.execute_sql(agg_sql, limit=1)
        row = agg["rows"][0] if agg["rows"] else {}
        total = int(row.get("total_rows") or 0)
        null_rows = int(row.get("null_rows") or 0)
        distinct = int(row.get("distinct_count") or 0)

        # 去重采样：样例值（≤10）与低基数枚举（≤25）
        sample_limit = max(10, ENUM_MAX_DISTINCT + 5)
        sample_sql = (
            f"SELECT DISTINCT {column} FROM {table} "
            f"WHERE {column} IS NOT NULL LIMIT {sample_limit}"
        )
        sample_res = await self.db_mcp.execute_sql(sample_sql, limit=sample_limit)
        values = [r.get(column) for r in sample_res["rows"]]

        profile: Dict[str, Any] = {
            "table": table,
            "column": column,
            "total_rows": total,
            "null_rate": round(null_rows / total, 4) if total else 0.0,
            "distinct_count": distinct,
            "sample_values": values[:10],
            "min_value": row.get("min_value"),
            "max_value": row.get("max_value"),
            "format_pattern": self._detect_format(values),
            "enum_values": values[:ENUM_MAX_DISTINCT] if 0 < distinct <= ENUM_MAX_DISTINCT else None,
        }
        return profile

    # ============================================
    # 格式识别（样例值多数派）
    # ============================================
    @staticmethod
    def _detect_format(values: List[Any]) -> Optional[str]:
        if not values:
            return None
        strs = [str(v) for v in values if v is not None]
        if not strs:
            return None
        for name, pattern in _FORMAT_PATTERNS.items():
            hits = sum(1 for s in strs if pattern.match(s.strip()))
            if hits / len(strs) >= _PATTERN_HIT_RATIO:
                return name
        # 金额：数值型样例，且含小数或数值量级大
        numeric = []
        for v in values:
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                numeric.append(float(v))
        if len(numeric) / len(values) >= _PATTERN_HIT_RATIO:
            if any(abs(x) >= _AMOUNT_MIN_ABS or (x != int(x)) for x in numeric):
                return "金额"
        return None


def profile_summary_text(profile: Dict[str, Any]) -> str:
    """把画像压缩成一句摘要，供 semantic 通道向量化使用"""
    if not profile or profile.get("error"):
        return ""
    parts = [f"空值率{profile.get('null_rate', 0):.0%}"]
    if profile.get("format_pattern"):
        parts.append(f"格式{profile['format_pattern']}")
    if profile.get("enum_values"):
        parts.append("枚举值" + "/".join(str(v) for v in profile["enum_values"][:8]))
    if profile.get("min_value") is not None:
        parts.append(f"值域{profile['min_value']}~{profile['max_value']}")
    return "，".join(parts)
