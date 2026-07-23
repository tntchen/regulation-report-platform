"""
审计日志集成测试（FastAPI TestClient）
覆盖：登录成功/失败产生 auth.login 审计、建任务产生 task.create 且记录真实用户名、
     匿名写操作 401 且审计 result=fail、审计查询 API 的分页与过滤

运行方式: python -m pytest tests/test_audit.py -v
注意: 与 test_auth.py 共用同一 settings 单例模式（先导入者生效），断言均做容差处理
"""

import os
import tempfile

# 在导入 app 之前切换到临时测试库 + 固定 JWT 密钥（与 test_auth.py 同模式）
_tmpdir = tempfile.mkdtemp(prefix="rrp_test_audit_")
os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{_tmpdir}/test_platform.db")
os.environ.setdefault("UPLOAD_DIR", f"{_tmpdir}/tenants")
os.environ.setdefault("DEMO_DB_PATH", f"{_tmpdir}/demo_biz.db")
os.environ.setdefault("TASK_WORK_DIR", f"{_tmpdir}/tasks")  # 交付物落临时目录，不污染 ./data/tasks
os.environ.setdefault("LOG_DIR", f"{_tmpdir}/logs")
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-pytest")
os.environ.setdefault("DEBUG", "false")

import pytest
from fastapi.testclient import TestClient

from backend.main import app


@pytest.fixture(scope="module")
def client():
    """TestClient 上下文触发 lifespan（建表 + 种子用户）"""
    with TestClient(app) as c:
        yield c


def login(client, username, password):
    return client.post("/v1/auth/login", json={"username": username, "password": password})


def auth_headers(client, username="admin", password="Admin@1234"):
    token = login(client, username, password).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def query_audit(client, headers, **params):
    qs = "&".join(f"{k}={v}" for k, v in params.items() if v is not None)
    r = client.get(f"/v1/tenants/T001/audit-logs?{qs}", headers=headers)
    assert r.status_code == 200
    return r.json()


# ---------- 审计产生 ----------

def test_login_success_writes_audit(client):
    """登录成功 → 产生 auth.login success 审计"""
    assert login(client, "admin", "Admin@1234").status_code == 200
    headers = auth_headers(client)
    data = query_audit(client, headers, action="auth.login", username="admin", result=None)
    # 至少存在一条 success 的 admin 登录记录
    assert any(l["action"] == "auth.login" and l["result"] == "success"
               and l["username"] == "admin" for l in data["logs"])
    # detail 里绝不出现密码字段
    for l in data["logs"]:
        assert "password" not in str(l["detail"]).lower() or l["detail"].get("password") == "***"


def test_login_failure_writes_audit(client):
    """登录失败 → 产生 auth.login fail 审计"""
    assert login(client, "zhangsan", "bad-password").status_code == 401
    headers = auth_headers(client)
    data = query_audit(client, headers, action="auth.login", username="zhangsan")
    assert any(l["result"] == "fail" for l in data["logs"])


def test_create_task_writes_audit_with_username(client):
    """zhangsan 建任务 → task.create 审计且 username=zhangsan"""
    headers = auth_headers(client, "zhangsan", "Zhangsan@1234")
    r = client.post("/v1/tenants/T001/tasks", headers=headers, json={
        "report_type": "1104", "report_code": "G01", "target_table": "g01_test",
    })
    assert r.status_code == 200
    task_id = r.json()["task_id"]

    data = query_audit(client, headers, action="task.create", username="zhangsan")
    matched = [l for l in data["logs"] if l["resource"] == task_id]
    assert matched, "未找到 task.create 审计"
    assert matched[0]["username"] == "zhangsan"
    assert matched[0]["detail"]["report_code"] == "G01"


# ---------- 匿名与越权 ----------

def test_anonymous_write_401_and_fail_audit(client):
    """匿名写操作 → 401，且审计中间件记录 result=fail"""
    r = client.post("/v1/tenants/T001/tasks", json={"report_type": "1104"})
    assert r.status_code == 401

    headers = auth_headers(client)
    data = query_audit(client, headers, action="http.write")
    failed = [l for l in data["logs"]
              if l["result"] == "fail" and "POST /v1/tenants/T001/tasks" in (l["resource"] or "")]
    assert failed, "未找到匿名写操作的 fail 审计"
    assert failed[0]["username"] is None  # 匿名：无用户身份


def test_audit_api_requires_auth(client):
    """审计查询接口本身挂鉴权：匿名 401、非成员 403"""
    assert client.get("/v1/tenants/T001/audit-logs").status_code == 401
    zhangsan_headers = auth_headers(client, "zhangsan", "Zhangsan@1234")
    assert client.get("/v1/tenants/T002/audit-logs", headers=zhangsan_headers).status_code == 403


# ---------- 分页与过滤 ----------

def test_audit_pagination_and_filter(client):
    """审计查询 API：分页 page_size 生效、action 过滤生效、actions 清单可用"""
    headers = auth_headers(client)

    data = query_audit(client, headers, page=1, page_size=5)
    assert data["page"] == 1
    assert len(data["logs"]) <= 5
    assert data["total"] > 0

    # action 过滤：返回的全部是该动作
    filtered = query_audit(client, headers, action="auth.login", page_size=50)
    assert filtered["total"] > 0
    assert all(l["action"] == "auth.login" for l in filtered["logs"])

    # actions 清单包含已产生的动作
    r = client.get("/v1/tenants/T001/audit-logs/actions", headers=headers)
    assert r.status_code == 200
    assert "auth.login" in r.json()["actions"]
