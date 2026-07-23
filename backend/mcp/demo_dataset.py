"""
演示数据集服务
为 Agent 4 测试验证 / Agent 5 数字孪生 提供真实可执行的 SQLite 演示数据。

数据设计（可解释差异）:
- 住房贷款（P001/P001-G）中多笔带 capitalized 利息 → EAST 口径余额 > 1104 口径余额
- 含 90 天临界两侧（30/92/95 天）的逾期样本
- 含 is_test=1 / is_deleted=1 / 其他机构(org_no='1002') 的"应被剔除"样本
- 含非住房产品（P002 消费贷）用于验证产品过滤

银行安全红线: 本模块仅提供只读 SELECT 执行能力，写操作仅限初始化种子与测试目标表。
"""

import os
import sqlite3
from typing import Dict, Any, List
from backend.config import settings


# 种子数据: 与 database_mcp 内存 Schema 保持一致
SEED_ROWS = [
    # contract_no, cust_id, product_code, loan_amount, principal_balance, interest_capitalized,
    # execute_rate, loan_status, repay_date, overdue_days, five_classify, biz_date, org_no, is_deleted, is_test
    ("C001", "U001", "P001",   1000000,  800000, 1200.00, 4.350000, "01", "2026-08-01",  0, "1", "2026-07-21", "1001", 0, 0),
    ("C002", "U002", "P001-G",  500000,  300000,    0.00, 3.100000, "01", "2026-08-05",  0, "1", "2026-07-21", "1001", 0, 0),
    ("C003", "U003", "P001",   2000000, 1500000, 5600.00, 4.900000, "02", "2026-06-15", 30, "2", "2026-07-21", "1001", 0, 0),
    ("C004", "U004", "P001",    800000,  600000, 2300.00, 5.200000, "02", "2026-04-10", 95, "3", "2026-07-21", "1001", 0, 0),
    ("C005", "U005", "P001",   1200000,  900000,    0.00, 4.050000, "01", "2026-07-30",  0, "1", "2026-07-21", "1001", 0, 0),
    ("C006", "U006", "P002",    300000,  200000,  500.00, 6.800000, "01", "2026-08-12",  0, "1", "2026-07-21", "1001", 0, 0),  # 消费贷，应被产品过滤剔除
    ("C007", "U007", "P001",    600000,  450000,  800.00, 4.600000, "02", "2026-04-20", 92, "2", "2026-07-21", "1001", 0, 0),
    ("C008", "U008", "P001",    700000,  500000, 1500.00, 4.450000, "01", "2026-08-20",  0, "1", "2026-07-21", "1001", 0, 0),
    ("C009", "U009", "P001",    900000,  700000,  900.00, 4.700000, "01", "2026-08-25",  0, "1", "2026-07-21", "1001", 0, 1),  # 测试数据，应剔除
    ("C010", "U010", "P001",    400000,  250000,  300.00, 4.500000, "01", "2026-08-28",  0, "1", "2026-07-21", "1001", 1, 0),  # 逻辑删除，应剔除
    ("C011", "U011", "P001",    550000,  400000,  600.00, 4.550000, "01", "2026-09-01",  0, "1", "2026-07-21", "1002", 0, 0),  # 其他机构，应剔除
    ("C012", "U012", "P001",   1500000, 1100000, 4200.00, 4.800000, "01", "2026-09-05",  0, "1", "2026-07-21", "1001", 0, 0),
]

COLUMNS = [
    "contract_no", "cust_id", "product_code", "loan_amount", "principal_balance",
    "interest_capitalized", "execute_rate", "loan_status", "repay_date",
    "overdue_days", "five_classify", "biz_date", "org_no", "is_deleted", "is_test"
]


class DemoDataset:
    """SQLite 演示数据集（单例式按路径管理）"""

    def __init__(self, db_path: str = None):
        self.db_path = db_path or settings.demo_db_path

    def ensure_seeded(self):
        """初始化种子数据（幂等）"""
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        try:
            cur = conn.cursor()
            cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='loan_contract'")
            if cur.fetchone():
                return  # 已初始化
            cur.execute("""
                CREATE TABLE loan_contract (
                    contract_no TEXT PRIMARY KEY,
                    cust_id TEXT NOT NULL,
                    product_code TEXT,
                    loan_amount REAL NOT NULL,
                    principal_balance REAL NOT NULL,
                    interest_capitalized REAL,
                    execute_rate REAL,
                    loan_status TEXT NOT NULL,
                    repay_date TEXT,
                    overdue_days INTEGER,
                    five_classify TEXT,
                    biz_date TEXT NOT NULL,
                    org_no TEXT NOT NULL,
                    is_deleted INTEGER NOT NULL DEFAULT 0,
                    is_test INTEGER NOT NULL DEFAULT 0
                )
            """)
            cur.executemany(
                f"INSERT INTO loan_contract ({', '.join(COLUMNS)}) VALUES ({', '.join(['?'] * len(COLUMNS))})",
                SEED_ROWS
            )
            conn.commit()
        finally:
            conn.close()

    def query(self, sql: str, params: tuple = ()) -> Dict[str, Any]:
        """执行只读 SELECT 查询（安全红线：仅允许 SELECT）"""
        if not sql.strip().upper().startswith("SELECT"):
            raise PermissionError("演示数据集仅允许 SELECT 只读查询")
        conn = sqlite3.connect(self.db_path)
        try:
            cur = conn.cursor()
            cur.execute(sql, params)
            columns = [d[0] for d in cur.description]
            rows = [dict(zip(columns, row)) for row in cur.fetchall()]
            return {"columns": columns, "rows": rows, "row_count": len(rows)}
        finally:
            conn.close()

    def execute_script(self, statements: List[str]):
        """执行写入类语句（仅限测试目标表初始化/数据装载，Agent 4 内部使用）"""
        conn = sqlite3.connect(self.db_path)
        try:
            cur = conn.cursor()
            for stmt in statements:
                cur.execute(stmt)
            conn.commit()
        finally:
            conn.close()

    def drop_table(self, table_name: str):
        """删除测试目标表（仅允许 rpt_/twin_ 前缀，防止误删业务表）"""
        if not (table_name.startswith("rpt_") or table_name.startswith("twin_")):
            raise PermissionError(f"仅允许删除 rpt_/twin_ 前缀的测试表: {table_name}")
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(f"DROP TABLE IF EXISTS {table_name}")
            conn.commit()
        finally:
            conn.close()

    def table_info(self, table_name: str) -> List[Dict[str, Any]]:
        """表结构元数据（PRAGMA table_info，仅供 schema 查询内部使用）"""
        conn = sqlite3.connect(self.db_path)
        try:
            cur = conn.execute(f"PRAGMA table_info({table_name})")
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
        finally:
            conn.close()

    # ============================================
    # 异步包装（L2-D6：同步 sqlite3 不阻塞事件循环）
    # ============================================
    async def aensure_seeded(self):
        """异步版 ensure_seeded（线程池执行，不阻塞事件循环）"""
        import asyncio
        await asyncio.to_thread(self.ensure_seeded)

    async def aquery(self, sql: str, params: tuple = ()) -> Dict[str, Any]:
        """异步版 query（线程池执行，不阻塞事件循环）"""
        import asyncio
        return await asyncio.to_thread(self.query, sql, params)

    async def aexecute_script(self, statements: List[str]):
        """异步版 execute_script（线程池执行，不阻塞事件循环）"""
        import asyncio
        await asyncio.to_thread(self.execute_script, statements)

    async def adrop_table(self, table_name: str):
        """异步版 drop_table（线程池执行，不阻塞事件循环）"""
        import asyncio
        await asyncio.to_thread(self.drop_table, table_name)

    async def atable_info(self, table_name: str) -> List[Dict[str, Any]]:
        """异步版 table_info（线程池执行，不阻塞事件循环）"""
        import asyncio
        return await asyncio.to_thread(self.table_info, table_name)


# 模块级共享实例
demo_dataset = DemoDataset()
