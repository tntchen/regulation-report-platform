"""
SQL 只读安全校验（第一层纵深：AST 白名单）
基于 sqlglot 语法解析，仅放行"单语句 SELECT（含 WITH...SELECT）"。

设计原则：
- 解析失败一律拒绝（不猜意图）
- 注释不影响判定（sqlglot 在词法层处理注释）
- 多语句、写操作、DDL/DCL、INTO OUTFILE/DUMPFILE、LOAD_FILE 等一律拒绝
- 危险函数（SLEEP/BENCHMARK/GET_LOCK 等时间/锁/系统函数）拒绝
"""

from typing import List

import sqlglot
from sqlglot import exp

# 危险函数黑名单（时间延迟/锁/文件/系统类）
DANGEROUS_FUNCTIONS = {
    "SLEEP", "BENCHMARK", "GET_LOCK", "RELEASE_LOCK", "IS_FREE_LOCK", "IS_USED_LOCK",
    "LOAD_FILE", "SYSDATE",  # SYSDATE 无参数副作用风险低，但方言差异大，统一拒绝以保持确定性
}

# 危险表达式类型（出现即拒绝）
DANGEROUS_EXPRESSIONS = (
    exp.Insert, exp.Update, exp.Delete, exp.Merge,
    exp.Drop, exp.Alter, exp.Create, exp.TruncateTable,
    exp.Grant, exp.Revoke,
    exp.Command,          # SET / USE / CALL / SHOW 等命令式语句
    exp.LoadData,         # LOAD DATA INFILE
    exp.Pragma,
    exp.Transaction, exp.Commit, exp.Rollback,
)


def _root_is_select(statement: exp.Expression) -> bool:
    """根语句是否为纯查询（SELECT / 集合运算 / 带 CTE 的查询）"""
    if isinstance(statement, (exp.Select, exp.Union, exp.Intersect, exp.Except)):
        return True
    # WITH ... SELECT：sqlglot 将 CTE 挂在 Select/Union 上；
    # WITH ... DELETE/UPDATE 则根类型是 Delete/Update，已被根类型拦截
    if isinstance(statement, exp.With):
        return _root_is_select(statement.this) if statement.this is not None else False
    return False


def validate_readonly_sql(sql: str) -> str:
    """校验 SQL 是否为允许的只读查询。
    通过返回原 SQL；拒绝时抛出 PermissionError（消息对调用方安全，不泄露解析细节）。"""
    if not sql or not sql.strip():
        raise PermissionError("SQL 为空")

    # 解析（不指定方言：平台查询以标准 SQL 为主；方言特性解析失败即拒绝）
    try:
        statements: List[exp.Expression] = [
            s for s in sqlglot.parse(sql) if s is not None
        ]
    except Exception:
        raise PermissionError("SQL 解析失败，已拒绝执行")

    # 仅允许单语句
    if len(statements) != 1:
        raise PermissionError("仅允许单条 SELECT 语句（检测到多语句）")

    stmt = statements[0]

    # 根语句必须是查询
    if not _root_is_select(stmt):
        raise PermissionError("仅允许 SELECT 只读查询")

    # 全树扫描危险表达式
    for node in stmt.walk():
        if isinstance(node, DANGEROUS_EXPRESSIONS):
            raise PermissionError(f"SQL 包含禁止的操作类型: {type(node).__name__}")
        # INTO OUTFILE / INTO DUMPFILE / SELECT INTO 新表
        if isinstance(node, exp.Into):
            raise PermissionError("SQL 包含禁止的 INTO 写文件/写表操作")
        # 危险函数
        if isinstance(node, exp.Anonymous):
            name = (node.name or "").upper()
            if name in DANGEROUS_FUNCTIONS:
                raise PermissionError(f"SQL 包含禁止的函数: {name}")
        elif isinstance(node, exp.Func):
            name = node.sql_name().upper()
            if name in DANGEROUS_FUNCTIONS:
                raise PermissionError(f"SQL 包含禁止的函数: {name}")

    return sql


def sanitize_db_error(err: Exception) -> str:
    """第三层护栏：数据库错误信息脱敏。
    不把原始错误（可能含库表结构/服务器版本细节）直接抛给调用方。"""
    msg = str(err)
    # 常见敏感片段粗过滤
    lowered = msg.lower()
    if "access denied" in lowered:
        return "数据库权限不足，操作被拒绝"
    if "unknown table" in lowered or "no such table" in lowered:
        return "查询引用的表不存在或不在授权范围内"
    if "syntax" in lowered:
        return "SQL 语法不被接受"
    if "timeout" in lowered or "timed out" in lowered:
        return "查询执行超时，已终止"
    return "查询执行失败（详细信息已记录服务端日志）"
