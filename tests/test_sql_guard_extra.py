"""
SQL 只读纵深 AST 边界补充用例（L2-Day10 补齐）
覆盖 test_sql_guard.py 未涉及的边界：集合运算放行、尾随分号/纯注释、
事务与 PRAGMA、危险函数嵌套、锁函数家族、错误脱敏各分支。

运行方式: python -m pytest tests/test_sql_guard_extra.py -v
"""

import pytest

from backend.utils.sql_guard import validate_readonly_sql, sanitize_db_error

# ============================================
# 放行边界
# ============================================

ALLOWED = [
    # 集合运算（Union 已覆盖，补 Intersect/Except）
    "SELECT a FROM t INTERSECT SELECT a FROM s",
    "SELECT a FROM t EXCEPT SELECT a FROM s",
    # 单语句尾随分号（仍属单语句）
    "SELECT 1;",
    # CTE + 集合运算组合
    "WITH x AS (SELECT 1 AS a) SELECT * FROM x UNION SELECT 2",
    # 子查询 IN
    "SELECT * FROM t WHERE id IN (SELECT id FROM s)",
    # 括号包裹的表达式
    "SELECT (1 + 2) * 3",
]

# ============================================
# 拒绝边界（现有用例未覆盖的绕过手法）
# ============================================

REJECTED = [
    # 锁函数家族（SLEEP/BENCHMARK/GET_LOCK 已覆盖）
    "SELECT RELEASE_LOCK('x')",
    "SELECT IS_FREE_LOCK('x')",
    "SELECT IS_USED_LOCK('x')",
    # 方言差异函数统一拒绝
    "SELECT SYSDATE()",
    # 危险函数嵌套在表达式/聚合内（全树扫描必须命中）
    "SELECT MAX(SLEEP(1)) FROM t",
    "SELECT IF(SLEEP(1), 1, 2)",
    "SELECT * FROM t WHERE x = BENCHMARK(100, MD5(1))",
    # 事务控制
    "BEGIN",
    "COMMIT",
    "ROLLBACK",
    "START TRANSACTION",
    # 元数据命令（sqlite PRAGMA 同样拒绝）
    "PRAGMA table_info(t)",
    "SHOW TABLES",
    # CREATE VIEW 也算 DDL
    "CREATE VIEW v AS SELECT 1",
    # 尾随分号后再挂语句（即便第二条是注释也视为多语句，宁严勿宽）
    "SELECT 1; SELECT 2;",
    # 纯注释：解析不出有效语句，拒绝
    "-- 只有一行注释",
]


@pytest.mark.parametrize("sql", ALLOWED)
def test_allowed_boundary(sql):
    assert validate_readonly_sql(sql) == sql


@pytest.mark.parametrize("sql", REJECTED)
def test_rejected_boundary(sql):
    with pytest.raises(PermissionError):
        validate_readonly_sql(sql)


def test_error_message_safe():
    """拒绝消息不泄露解析树细节，只给操作类型"""
    with pytest.raises(PermissionError) as exc:
        validate_readonly_sql("DROP TABLE t")
    assert "DROP" not in str(exc.value).upper() or "禁止" in str(exc.value)


# ============================================
# sanitize_db_error 各分支
# ============================================

def test_sanitize_access_denied():
    assert sanitize_db_error(Exception("Access denied for user")) == "数据库权限不足，操作被拒绝"


def test_sanitize_unknown_table():
    assert sanitize_db_error(Exception("no such table: secret")) == "查询引用的表不存在或不在授权范围内"
    assert sanitize_db_error(Exception("Unknown table 'x'")) == "查询引用的表不存在或不在授权范围内"


def test_sanitize_syntax():
    assert sanitize_db_error(Exception("You have an error in your SQL syntax")) == "SQL 语法不被接受"


def test_sanitize_timeout():
    assert sanitize_db_error(Exception("Query execution timed out")) == "查询执行超时，已终止"
    assert sanitize_db_error(Exception("lock wait timeout exceeded")) == "查询执行超时，已终止"


def test_sanitize_generic_no_leak():
    """未知错误统一兜底，不透出原始细节（可能含库表/版本信息）"""
    msg = sanitize_db_error(Exception("server version: 8.0.32 MySQL, db=core_banking"))
    assert "8.0.32" not in msg and "core_banking" not in msg
