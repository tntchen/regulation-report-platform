"""
数据探查 API 测试（GET /report-packs/{pack_id}/profile?table=...）
覆盖：全字段画像响应结构、画像数值与演示数据集一致、
     源表白名单外 400、未认证 401、包不存在 404、审计落库

运行方式: python -m pytest tests/test_profile_api.py -v
"""

import asyncio
import os
import tempfile

# 在导入 app 之前切换到临时测试库 + 固定 JWT 密钥（与其他测试同模式）
_tmpdir = tempfile.mkdtemp(prefix="rrp_test_profile_")
os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{_tmpdir}/test_platform.db")
os.environ.setdefault("UPLOAD_DIR", f"{_tmpdir}/tenants")
os.environ.setdefault("DEMO_DB_PATH", f"{_tmpdir}/demo_biz.db")
os.environ.setdefault("TASK_WORK_DIR", f"{_tmpdir}/tasks")
os.environ.setdefault("LOG_DIR", f"{_tmpdir}/logs")
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-pytest")
os.environ.setdefault("DEBUG", "false")

import pytest
from fastapi.testclient import TestClient

from backend.config import settings
from backend.main import app
from backend.api.report_packs import report_packs_router
from backend.services import report_pack_service

settings.task_worker_enabled = False

# 路由注册由协调者统一做（main.py 归其他范围）；测试内手动挂载本范围路由
app.include_router(report_packs_router, prefix="/v1")


@pytest.fixture(scope="module")
def client():
    """TestClient 上下文触发 lifespan（建表 + 种子用户），并灌入内置场景包"""
    with TestClient(app) as c:
        asyncio.run(report_pack_service.seed_builtin_packs())
        yield c


def login(client, username="admin", password="Admin@1234"):
    r = client.post("/v1/auth/login", json={"username": username, "password": password})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


PROFILE_URL = "/v1/tenants/T001/report-packs/G01/profile"


# ---------- 正常画像 ----------

def test_profile_response_structure(client):
    """响应结构符合契约：{table, columns: [{column_name, data_type, null_rate,
    distinct_count, sample_values, format_pattern, enum_values, total_rows}]}"""
    r = client.get(PROFILE_URL, params={"table": "loan_contract"}, headers=login(client))
    assert r.status_code == 200
    body = r.json()
    assert body["table"] == "loan_contract"
    assert isinstance(body["columns"], list) and body["columns"]

    expected_keys = {"column_name", "data_type", "null_rate", "distinct_count",
                     "sample_values", "format_pattern", "enum_values", "total_rows"}
    names = set()
    for col in body["columns"]:
        assert set(col.keys()) == expected_keys
        names.add(col["column_name"])
    # 演示数据集 loan_contract 全字段覆盖
    assert {"contract_no", "cust_id", "product_code", "loan_amount",
            "principal_balance", "five_classify", "biz_date", "org_no"} <= names


def test_profile_values_match_demo_dataset(client):
    """画像数值与演示数据集一致：12 行种子、五级分类 3 枚举、机构号 2 枚举等"""
    body = client.get(PROFILE_URL, params={"table": "loan_contract"},
                      headers=login(client)).json()
    cols = {c["column_name"]: c for c in body["columns"]}

    # 全表 12 行，且种子数据无空值
    assert all(c["total_rows"] == 12 for c in cols.values())
    assert all(c["null_rate"] == 0.0 for c in cols.values())

    # 主键合同号 12 个去重
    assert cols["contract_no"]["distinct_count"] == 12
    assert len(cols["contract_no"]["sample_values"]) == 10  # 样例上限 10

    # 低基数字段输出枚举：五级分类 {1,2,3}、机构号 {1001,1002}
    assert cols["five_classify"]["distinct_count"] == 3
    assert sorted(cols["five_classify"]["enum_values"]) == ["1", "2", "3"]
    assert sorted(cols["org_no"]["enum_values"]) == ["1001", "1002"]

    # 高基数字段不给枚举
    assert cols["contract_no"]["enum_values"] is None

    # 格式识别：金额为数值大量级、业务日期命中日期模式
    assert cols["loan_amount"]["format_pattern"] == "金额"
    assert cols["biz_date"]["format_pattern"] == "日期"


def test_profile_audit_written(client):
    """成功画像写审计 report_pack.profile"""
    headers = login(client)
    client.get(PROFILE_URL, params={"table": "loan_contract"}, headers=headers)
    logs = client.get("/v1/tenants/T001/audit-logs?action=report_pack.profile",
                      headers=headers).json()
    assert any(l["resource"] == "G01" for l in logs["logs"])


# ---------- 白名单 / 鉴权 ----------

def test_profile_table_not_in_whitelist(client):
    """表白名单外 → 400（防任意表探测）；缺 table 参数 → 422"""
    headers = login(client)
    r = client.get(PROFILE_URL, params={"table": "secret_salary"}, headers=headers)
    assert r.status_code == 400
    assert "白名单" in r.json()["detail"]

    # 目标表是输出表、不在 source_tables 中，同样拒绝
    r = client.get(PROFILE_URL, params={"table": "rpt_g01_housing_loan"}, headers=headers)
    assert r.status_code == 400

    # 缺 query 参数 → FastAPI 422
    assert client.get(PROFILE_URL, headers=headers).status_code == 422


def test_profile_auth_and_pack_errors(client):
    """未认证 401；包不存在 404；租户成员可读（非 admin 也可画像）"""
    # 未认证 → 401
    assert client.get(PROFILE_URL, params={"table": "loan_contract"}).status_code == 401

    # 包不存在 → 404
    r = client.get("/v1/tenants/T001/report-packs/NOPE/profile",
                   params={"table": "loan_contract"}, headers=login(client))
    assert r.status_code == 404

    # 普通租户成员（非 admin）可读画像
    z_headers = login(client, "zhangsan", "Zhangsan@1234")
    r = client.get(PROFILE_URL, params={"table": "loan_contract"}, headers=z_headers)
    assert r.status_code == 200
