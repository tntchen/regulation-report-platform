"""
MCP服务: database_mcp（L2-D6 真实化改造）
职责: 数据库 Schema 查询 + 只读 SQL 执行

只读三层纵深：
  第一层 AST 白名单（utils/sql_guard.py）：sqlglot 解析，仅单语句 SELECT；
  第二层 数据库侧最小权限：生产 MySQL 用 readonly 账号连接（见 scripts/seed_mysql.py），
         演示环境用 SQLite 演示数据集（物理上无业务库可写）；
  第三层 执行护栏：语句超时（默认 10s）+ 结果行数上限 + 错误信息脱敏。

方言支持：
  - sqlite_demo：离线演示路径（默认），真实执行于演示数据集
  - mysql：生产路径（需 Docker MySQL + aiomysql/asyncmy 驱动），走 information_schema
  - oracle/gaussdb：适配器扩展点，暂未实现（配置时会明确报错而非静默失败）
"""

import asyncio
from typing import Dict, Any

from backend.config import settings
from backend.utils.sql_guard import validate_readonly_sql, sanitize_db_error
from backend.utils.logging import get_logger

logger = get_logger(__name__)

# 语句执行超时（秒）
STATEMENT_TIMEOUT = 10


class DatabaseMCPService:
    """数据库MCP服务"""

    def __init__(self, db_config: dict = None):
        self.db_config = db_config or {}
        self.db_type = self.db_config.get("db_type", "sqlite_demo")
        self.readonly = self.db_config.get("readonly", True)
        self.whitelist_tables = self.db_config.get("whitelist_tables", [])
        self.max_limit = settings.mcp_max_limit

    # ============================================
    # Schema 查询
    # ============================================
    async def query_schema(self, table_name: str, schema: str = None) -> Dict[str, Any]:
        """获取表结构（白名单校验后走真实元数据）"""
        if self.whitelist_tables and table_name not in self.whitelist_tables:
            raise PermissionError(f"表 {table_name} 不在白名单中")

        if self.db_type == "sqlite_demo":
            return await self._query_schema_sqlite(table_name)
        if self.db_type == "mysql":
            return await self._query_schema_mysql(table_name, schema)
        raise ValueError(
            f"数据源类型 {self.db_type} 暂未实现（方言扩展点：当前支持 sqlite_demo / mysql）"
        )

    async def _query_schema_sqlite(self, table_name: str) -> Dict[str, Any]:
        """SQLite 演示数据集：PRAGMA 读取真实表结构"""
        from backend.mcp.demo_dataset import demo_dataset

        await demo_dataset.aensure_seeded()
        cols = await demo_dataset.atable_info(table_name)
        if not cols:
            return {"table_name": table_name, "schema": "demo", "columns": [], "indexes": []}

        type_map = {"contract_no": "VARCHAR(32)", "cust_id": "VARCHAR(20)"}
        return {
            "table_name": table_name,
            "schema": "demo",
            "columns": [
                {
                    "column_name": c["name"],
                    "data_type": type_map.get(c["name"], (c["type"] or "TEXT").upper()),
                    "is_nullable": "NO" if c["notnull"] else "YES",
                    "column_comment": "",
                    "is_pk": bool(c["pk"]),
                    "is_index": bool(c["pk"]),
                }
                for c in cols
            ],
            "indexes": [],
        }

    async def _query_schema_mysql(self, table_name: str, schema: str = None) -> Dict[str, Any]:
        """MySQL 生产路径：information_schema 读取真实表结构"""
        from sqlalchemy import text
        from backend.database import get_tenant_engine

        database = schema or self.db_config.get("database", "")
        try:
            engine = get_tenant_engine(self._mysql_url())
        except Exception as e:
            raise RuntimeError(f"MySQL 连接不可用（需 Docker 环境与异步驱动）: {sanitize_db_error(e)}")

        sql = text(
            "SELECT COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLUMN_KEY, COLUMN_COMMENT "
            "FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = :db AND TABLE_NAME = :tbl ORDER BY ORDINAL_POSITION"
        )
        try:
            async with engine.connect() as conn:
                rows = (await conn.execute(sql, {"db": database, "tbl": table_name})).mappings().all()
        except Exception as e:
            logger.error("information_schema 查询失败: %s", e)
            raise RuntimeError(f"表结构查询失败: {sanitize_db_error(e)}")

        return {
            "table_name": table_name,
            "schema": database,
            "columns": [
                {
                    "column_name": r["COLUMN_NAME"],
                    "data_type": r["COLUMN_TYPE"].upper(),
                    "is_nullable": r["IS_NULLABLE"],
                    "column_comment": r["COLUMN_COMMENT"] or "",
                    "is_pk": r["COLUMN_KEY"] == "PRI",
                    "is_index": r["COLUMN_KEY"] in ("PRI", "MUL"),
                }
                for r in rows
            ],
            "indexes": [],
        }

    # ============================================
    # 只读 SQL 执行
    # ============================================
    async def execute_sql(self, sql: str, limit: int = 100) -> Dict[str, Any]:
        """执行只读 SQL（三层纵深：AST 白名单 → 只读连接 → 超时/行限/脱敏）"""
        # 第一层：AST 白名单
        validate_readonly_sql(sql)

        # 第三层：行数上限
        limit = min(limit, self.max_limit)

        if self.db_type == "sqlite_demo":
            return await self._execute_sqlite(sql, limit)
        if self.db_type == "mysql":
            return await self._execute_mysql(sql, limit)
        raise ValueError(
            f"数据源类型 {self.db_type} 暂未实现（方言扩展点：当前支持 sqlite_demo / mysql）"
        )

    async def _execute_sqlite(self, sql: str, limit: int) -> Dict[str, Any]:
        """SQLite 演示数据集真实执行（线程池 + 超时 + 行限）"""
        from backend.mcp.demo_dataset import demo_dataset

        await demo_dataset.aensure_seeded()
        start = asyncio.get_event_loop().time()
        try:
            # 第三层：语句超时
            result = await asyncio.wait_for(
                demo_dataset.aquery(sql), timeout=STATEMENT_TIMEOUT
            )
        except asyncio.TimeoutError:
            raise TimeoutError("查询执行超时，已终止")
        except PermissionError:
            raise
        except Exception as e:
            logger.error("SQL 执行失败: %s | SQL: %s", e, sql[:200])
            raise RuntimeError(sanitize_db_error(e))

        rows = result["rows"][:limit]
        return {
            "columns": result["columns"],
            "rows": rows,
            "row_count": len(rows),
            "truncated": result["row_count"] > limit,
            "execution_time_ms": int((asyncio.get_event_loop().time() - start) * 1000),
        }

    async def _execute_mysql(self, sql: str, limit: int) -> Dict[str, Any]:
        """MySQL 生产路径真实执行（连接池 + 超时 + 行限）"""
        from sqlalchemy import text
        from backend.database import get_tenant_engine

        try:
            engine = get_tenant_engine(self._mysql_url())
        except Exception as e:
            raise RuntimeError(f"MySQL 连接不可用（需 Docker 环境与异步驱动）: {sanitize_db_error(e)}")

        start = asyncio.get_event_loop().time()
        try:
            async with engine.connect() as conn:
                result = await asyncio.wait_for(
                    conn.execute(text(sql)), timeout=STATEMENT_TIMEOUT
                )
                columns = list(result.keys())
                rows = [dict(zip(columns, r)) for r in result.fetchmany(limit)]
        except asyncio.TimeoutError:
            raise TimeoutError("查询执行超时，已终止")
        except Exception as e:
            logger.error("MySQL 执行失败: %s | SQL: %s", e, sql[:200])
            raise RuntimeError(sanitize_db_error(e))

        return {
            "columns": columns,
            "rows": rows,
            "row_count": len(rows),
            "truncated": len(rows) >= limit,
            "execution_time_ms": int((asyncio.get_event_loop().time() - start) * 1000),
        }

    def _mysql_url(self) -> str:
        """构建 MySQL 异步连接 URL（优先 asyncmy，其次 aiomysql）"""
        host = self.db_config.get("host", "localhost")
        port = self.db_config.get("port", 3306)
        database = self.db_config.get("database", "")
        username = self.db_config.get("username", "")
        password = self.db_config.get("password", "")
        try:
            import asyncmy  # noqa: F401
            driver = "asyncmy"
        except ImportError:
            driver = "aiomysql"
        return f"mysql+{driver}://{username}:{password}@{host}:{port}/{database}"
