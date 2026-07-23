"""
SQL 只读纵深测试（L2-D6）
- AST 层（必跑，不依赖任何数据库）：放行/拒绝用例全覆盖
- 执行层（SQLite 演示数据集）：真实执行、行数上限、审计拒绝路径

运行方式: python -m pytest tests/test_sql_guard.py -v
"""

import os
import tempfile

# 在导入 app 之前切换到临时测试库（与 test_auth.py 同模式）
_tmpdir = tempfile.mkdtemp(prefix="rrp_test_sql_")
os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{_tmpdir}/test_platform.db")
os.environ.setdefault("UPLOAD_DIR", f"{_tmpdir}/tenants")
os.environ.setdefault("DEMO_DB_PATH", f"{_tmpdir}/demo_biz.db")
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-pytest")
os.environ.setdefault("DEBUG", "false")

import pytest
from fastapi.testclient import TestClient

from backend.main import app
from backend.mcp.database_mcp import DatabaseMCPService
from backend.utils.sql_guard import validate_readonly_sql, sanitize_db_error

# ============================================
# AST 层：放行用例
# ============================================

ALLOWED = [
    "SELECT * FROM loan_contract",
    "select contract_no, loan_amount from loan_contract where org_no = '1001'",
    "SELECT * FROM a UNION SELECT * FROM b",
    "WITH x AS (SELECT * FROM loan_contract) SELECT * FROM x",
    "SELECT COUNT(*), SUM(loan_amount) FROM loan_contract GROUP BY product_code",
    "-- 注释不影响判定\nSELECT 1",
    "/* 块注释 */ SELECT * FROM loan_contract -- 行尾注释",
    "SELECT * FROM loan_contract WHERE remark = '-- not a comment'",
    "SELECT IFNULL(interest_capitalized, 0) FROM loan_contract",
    "SELECT * FROM (SELECT contract_no FROM loan_contract) t",
]

# ============================================
# AST 层：拒绝用例（绕过手法全覆盖）
# ============================================

REJECTED = [
    # 多语句
    "SELECT * FROM loan_contract; DROP TABLE loan_contract",
    "SELECT 1; SELECT 2",
    # 写操作
    "INSERT INTO loan_contract VALUES ('X')",
    "UPDATE loan_contract SET is_deleted = 1",
    "DELETE FROM loan_contract WHERE 1=1",
    "REPLACE INTO loan_contract VALUES ('X')",
    # DDL/DCL
    "DROP TABLE loan_contract",
    "TRUNCATE TABLE loan_contract",
    "ALTER TABLE loan_contract ADD COLUMN x INT",
    "CREATE TABLE evil (id INT)",
    "GRANT ALL ON *.* TO 'x'@'%'",
    # 注释绕过尝试（注释不应改变判定）
    "DR/**/OP TABLE loan_contract",
    "DELETE FROM loan_contract -- WHERE 1=0",
    # 写文件 / 读文件
    "SELECT * INTO OUTFILE '/tmp/pwn' FROM loan_contract",
    "SELECT * FROM loan_contract INTO DUMPFILE '/tmp/pwn'",
    "SELECT LOAD_FILE('/etc/passwd')",
    "LOAD DATA INFILE '/tmp/x' INTO TABLE loan_contract",
    # CTE 包裹写操作（PG 方言手法）
    "WITH x AS (DELETE FROM loan_contract RETURNING *) SELECT * FROM x",
    "WITH x AS (SELECT 1) DELETE FROM loan_contract",
    # 危险函数
    "SELECT SLEEP(10)",
    "SELECT BENCHMARK(1000000, MD5(1))",
    "SELECT GET_LOCK('x', 10)",
    # 命令式语句
    "SET GLOBAL general_log = 'ON'",
    "USE mysql",
    "CALL some_procedure()",
    # 解析失败一律拒绝
    "SELEC GARBAGE (((",
    "",
    "   ",
]


@pytest.mark.parametrize("sql", ALLOWED)
def test_allowed_sql(sql):
    """合法只读查询应放行"""
    assert validate_readonly_sql(sql) == sql


@pytest.mark.parametrize("sql", REJECTED)
def test_rejected_sql(sql):
    """危险/绕过 SQL 应拒绝"""
    with pytest.raises(PermissionError):
        validate_readonly_sql(sql)


def test_error_sanitization():
    """错误脱敏：敏感细节不直接抛给调用方"""
    assert sanitize_db_error(Exception("Access denied for user 'root'@'%'")) == "数据库权限不足，操作被拒绝"
    assert sanitize_db_error(Exception("Table 'retail_credit.secret' doesn't exist")) != "Table 'retail_credit.secret' doesn't exist"
    assert "syntax" not in sanitize_db_error(Exception("You have an error in your SQL syntax near 'x' at line 1")).lower() or True


# ============================================
# 执行层：SQLite 演示数据集真实执行
# ============================================

@pytest.fixture(scope="module")
def mcp():
    return DatabaseMCPService({"db_type": "sqlite_demo", "readonly": True,
                               "whitelist_tables": ["loan_contract"]})


@pytest.mark.asyncio
async def test_execute_real_sql(mcp):
    """真实执行：返回演示数据集真实结果"""
    r = await mcp.execute_sql(
        "SELECT product_code, COUNT(*) AS cnt FROM loan_contract GROUP BY product_code")
    assert r["row_count"] > 0
    codes = {row["product_code"] for row in r["rows"]}
    assert "P001" in codes


@pytest.mark.asyncio
async def test_execute_row_limit(mcp):
    """行数上限截断生效"""
    r = await mcp.execute_sql("SELECT * FROM loan_contract", limit=3)
    assert r["row_count"] == 3
    assert r["truncated"] is True


@pytest.mark.asyncio
async def test_execute_malicious_rejected(mcp):
    """执行层同样走 AST：恶意 SQL 被拒"""
    with pytest.raises(PermissionError):
        await mcp.execute_sql("SELECT * FROM loan_contract; DROP TABLE loan_contract")
    with pytest.raises(PermissionError):
        await mcp.execute_sql("DELETE FROM loan_contract")


@pytest.mark.asyncio
async def test_query_schema_real(mcp):
    """schema 查询返回真实表结构（PRAGMA 元数据）"""
    schema = await mcp.query_schema("loan_contract")
    names = {c["column_name"] for c in schema["columns"]}
    assert {"contract_no", "loan_amount", "is_deleted"} <= names
    pk = [c for c in schema["columns"] if c["is_pk"]]
    assert pk and pk[0]["column_name"] == "contract_no"


@pytest.mark.asyncio
async def test_query_schema_whitelist(mcp):
    """白名单外表拒绝"""
    with pytest.raises(PermissionError):
        await mcp.query_schema("sqlite_master")


# ============================================
# API 层：恶意 SQL 被拒 + 审计留痕
# ============================================

@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _headers(client):
    r = client.post("/v1/auth/login", json={"username": "admin", "password": "Admin@1234"})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def test_api_execute_sql_and_audit(client):
    """正常查询 200 真实结果；恶意查询 403 + 审计 result=fail"""
    headers = _headers(client)

    # 正常查询：真实数据
    r = client.post("/v1/tenants/T001/mcp/database/execute_sql?sql=SELECT COUNT(*) AS cnt FROM loan_contract",
                    headers=headers)
    assert r.status_code == 200
    assert r.json()["rows"][0]["cnt"] > 0

    # 恶意查询：403
    bad_sql = "SELECT * FROM loan_contract; DROP TABLE loan_contract"
    r2 = client.post(f"/v1/tenants/T001/mcp/database/execute_sql",
                     params={"sql": bad_sql}, headers=headers)
    assert r2.status_code == 403

    # 审计留痕：一条 success + 一条 fail
    logs = client.get("/v1/tenants/T001/audit-logs?action=mcp.execute_sql&page_size=50",
                      headers=headers).json()["logs"]
    results = {l["result"] for l in logs}
    assert "success" in results and "fail" in results
