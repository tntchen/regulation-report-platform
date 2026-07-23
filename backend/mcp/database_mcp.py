"""
MCP服务: database_mcp
职责: 数据库Schema查询 + 只读SQL执行
"""

import re
from typing import Dict, Any, List
from backend.config import settings


class DatabaseMCPService:
    """数据库MCP服务"""

    def __init__(self, db_config: dict = None):
        self.db_config = db_config or {}
        self.readonly = self.db_config.get("readonly", True)
        self.whitelist_tables = self.db_config.get("whitelist_tables", [])
        self.max_limit = settings.mcp_max_limit

    async def query_schema(self, table_name: str, schema: str = None) -> Dict[str, Any]:
        """获取表结构"""
        # 安全检查
        if self.whitelist_tables and table_name not in self.whitelist_tables:
            raise PermissionError(f"表 {table_name} 不在白名单中")

        # 模拟返回（实际应查询数据库）
        # 这里返回预置的loan_contract表结构
        if table_name == "loan_contract":
            return {
                "table_name": table_name,
                "schema": schema or "retail_credit",
                "columns": [
                    {"column_name": "contract_no", "data_type": "VARCHAR(32)", "is_nullable": "NO", "column_comment": "合同编号", "is_pk": True, "is_index": True},
                    {"column_name": "cust_id", "data_type": "VARCHAR(20)", "is_nullable": "NO", "column_comment": "客户ID", "is_pk": False, "is_index": True},
                    {"column_name": "product_code", "data_type": "VARCHAR(10)", "is_nullable": "YES", "column_comment": "产品代码", "is_pk": False, "is_index": False},
                    {"column_name": "loan_amount", "data_type": "DECIMAL(18,2)", "is_nullable": "NO", "column_comment": "贷款金额(元)", "is_pk": False, "is_index": False},
                    {"column_name": "principal_balance", "data_type": "DECIMAL(18,2)", "is_nullable": "NO", "column_comment": "本金余额", "is_pk": False, "is_index": False},
                    {"column_name": "interest_capitalized", "data_type": "DECIMAL(18,2)", "is_nullable": "YES", "column_comment": "资本化利息", "is_pk": False, "is_index": False},
                    {"column_name": "execute_rate", "data_type": "DECIMAL(10,6)", "is_nullable": "YES", "column_comment": "执行利率", "is_pk": False, "is_index": False},
                    {"column_name": "loan_status", "data_type": "VARCHAR(2)", "is_nullable": "NO", "column_comment": "贷款状态", "is_pk": False, "is_index": True},
                    {"column_name": "repay_date", "data_type": "DATE", "is_nullable": "YES", "column_comment": "应还日期", "is_pk": False, "is_index": False},
                    {"column_name": "overdue_days", "data_type": "INT", "is_nullable": "YES", "column_comment": "逾期天数", "is_pk": False, "is_index": False},
                    {"column_name": "five_classify", "data_type": "VARCHAR(1)", "is_nullable": "YES", "column_comment": "五级分类", "is_pk": False, "is_index": False},
                    {"column_name": "biz_date", "data_type": "DATE", "is_nullable": "NO", "column_comment": "业务日期", "is_pk": False, "is_index": True},
                    {"column_name": "org_no", "data_type": "VARCHAR(10)", "is_nullable": "NO", "column_comment": "机构号", "is_pk": False, "is_index": True},
                    {"column_name": "is_deleted", "data_type": "TINYINT(1)", "is_nullable": "NO", "column_default": "0", "column_comment": "是否删除", "is_pk": False, "is_index": False},
                    {"column_name": "is_test", "data_type": "TINYINT(1)", "is_nullable": "NO", "column_default": "0", "column_comment": "是否测试", "is_pk": False, "is_index": False}
                ],
                "indexes": [
                    {"index_name": "idx_contract_no", "column_names": ["contract_no"], "is_unique": True},
                    {"index_name": "idx_biz_date_org", "column_names": ["biz_date", "org_no"], "is_unique": False}
                ]
            }

        elif table_name == "customer_info":
            return {
                "table_name": table_name,
                "schema": schema or "retail_credit",
                "columns": [
                    {"column_name": "cust_id", "data_type": "VARCHAR(20)", "is_nullable": "NO", "column_comment": "客户ID", "is_pk": True, "is_index": True},
                    {"column_name": "cust_name", "data_type": "VARCHAR(100)", "is_nullable": "YES", "column_comment": "客户姓名", "is_pk": False, "is_index": False},
                    {"column_name": "id_card", "data_type": "VARCHAR(18)", "is_nullable": "YES", "column_comment": "身份证号", "is_pk": False, "is_index": False},
                    {"column_name": "phone", "data_type": "VARCHAR(20)", "is_nullable": "YES", "column_comment": "手机号", "is_pk": False, "is_index": False}
                ],
                "indexes": [
                    {"index_name": "idx_cust_id", "column_names": ["cust_id"], "is_unique": True}
                ]
            }

        return {"table_name": table_name, "columns": [], "indexes": []}

    async def execute_sql(self, sql: str, limit: int = 100) -> Dict[str, Any]:
        """执行只读SQL"""
        # 安全检查1: 必须是SELECT
        sql_upper = sql.strip().upper()
        if not sql_upper.startswith("SELECT"):
            raise PermissionError("只允许执行SELECT查询")

        # 安全检查2: 禁止危险关键字
        forbidden_keywords = ["INSERT", "UPDATE", "DELETE", "DROP", "TRUNCATE", "ALTER", "CREATE", "GRANT", "INTO OUTFILE", "LOAD_FILE"]
        for keyword in forbidden_keywords:
            if keyword in sql_upper:
                raise PermissionError(f"SQL包含禁止关键字: {keyword}")

        # 安全检查3: 限制返回行数
        if limit > self.max_limit:
            limit = self.max_limit

        # 模拟执行（实际应连接数据库）
        # 返回模拟数据
        return {
            "columns": ["product_code", "cnt"],
            "rows": [
                {"product_code": "P001", "cnt": 500},
                {"product_code": "P001-G", "cnt": 80},
                {"product_code": "P002", "cnt": 250},
                {"product_code": "P003", "cnt": 120},
                {"product_code": "P004", "cnt": 50}
            ],
            "row_count": 5,
            "execution_time_ms": 45
        }
