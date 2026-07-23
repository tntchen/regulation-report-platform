"""
MySQL 演示库种子脚本（L2-D6，需 Docker MySQL 环境）
用途：
  1. 创建只读账号 mcp_readonly（仅 SELECT 权限，第二层纵深）
  2. 建 loan_contract 表（含注释，与演示口径一致）
  3. 灌入与 backend/mcp/demo_dataset.py 相同口径的种子数据
     （资本化利息、逾期临界 30/92/95 天、is_test/is_deleted/跨机构样本）

用法（MySQL 容器起来之后）：
  python scripts/seed_mysql.py --host 127.0.0.1 --port 3306 \
      --root-password root123 --database retail_credit

依赖：pymysql（同步驱动，仅种子脚本使用；运行时执行走异步驱动）
"""

import argparse
import sys

# 与 backend/mcp/demo_dataset.py 保持一致的种子数据
sys.path.insert(0, ".")
from backend.mcp.demo_dataset import SEED_ROWS, COLUMNS  # noqa: E402

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS loan_contract (
    contract_no VARCHAR(32) PRIMARY KEY COMMENT '合同编号',
    cust_id VARCHAR(20) NOT NULL COMMENT '客户ID',
    product_code VARCHAR(10) COMMENT '产品代码',
    loan_amount DECIMAL(18,2) NOT NULL COMMENT '贷款金额(元)',
    principal_balance DECIMAL(18,2) NOT NULL COMMENT '本金余额',
    interest_capitalized DECIMAL(18,2) COMMENT '资本化利息',
    execute_rate DECIMAL(10,6) COMMENT '执行利率',
    loan_status VARCHAR(2) NOT NULL COMMENT '贷款状态',
    repay_date DATE COMMENT '应还日期',
    overdue_days INT COMMENT '逾期天数',
    five_classify VARCHAR(1) COMMENT '五级分类',
    biz_date DATE NOT NULL COMMENT '业务日期',
    org_no VARCHAR(10) NOT NULL COMMENT '机构号',
    is_deleted TINYINT(1) NOT NULL DEFAULT 0 COMMENT '是否删除',
    is_test TINYINT(1) NOT NULL DEFAULT 0 COMMENT '是否测试',
    INDEX idx_biz_date_org (biz_date, org_no),
    INDEX idx_cust_id (cust_id)
) COMMENT='零售信贷借据表(演示)'
"""

READONLY_USER_SQL = [
    # 只读账号（第二层纵深：平台连接仅用此账号，root 不进配置）
    "CREATE USER IF NOT EXISTS 'mcp_readonly'@'%' IDENTIFIED BY 'readonly123'",
    "GRANT SELECT ON {db}.* TO 'mcp_readonly'@'%'",
    "FLUSH PRIVILEGES",
]


def main():
    parser = argparse.ArgumentParser(description="MySQL 演示库种子导入")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=3306)
    parser.add_argument("--root-password", default="root123")
    parser.add_argument("--database", default="retail_credit")
    args = parser.parse_args()

    try:
        import pymysql
    except ImportError:
        print("缺少依赖: pip install pymysql")
        return 1

    conn = pymysql.connect(host=args.host, port=args.port, user="root",
                           password=args.root_password, database=args.database,
                           charset="utf8mb4")
    try:
        with conn.cursor() as cur:
            # 1. 只读账号
            for sql in READONLY_USER_SQL:
                cur.execute(sql.format(db=args.database))
            print("只读账号 mcp_readonly 已创建/授权（仅 SELECT）")

            # 2. 建表
            cur.execute(CREATE_TABLE_SQL)
            print("loan_contract 表已创建")

            # 3. 种子数据（幂等：先清后灌）
            cur.execute("DELETE FROM loan_contract")
            placeholders = ", ".join(["%s"] * len(COLUMNS))
            cur.executemany(
                f"INSERT INTO loan_contract ({', '.join(COLUMNS)}) VALUES ({placeholders})",
                SEED_ROWS,
            )
            print(f"种子数据已灌入: {len(SEED_ROWS)} 行")
        conn.commit()
    finally:
        conn.close()

    print("✅ MySQL 演示库种子导入完成")
    print("   平台数据源配置: db_type=mysql, username=mcp_readonly, password=readonly123")
    return 0


if __name__ == "__main__":
    sys.exit(main())
