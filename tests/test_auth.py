"""
认证与租户鉴权集成测试（FastAPI TestClient）
覆盖：登录成功/失败、无 token 401、伪造/过期 token 401、跨租户 403、list_tasks 不再越权

运行方式: python -m pytest tests/test_auth.py -v
注意: 使用独立的测试数据库（环境变量 DATABASE_URL 指向临时文件），不污染开发数据
"""

import os
import tempfile

# 在导入 app 之前切换到临时测试库 + 固定 JWT 密钥
_tmpdir = tempfile.mkdtemp(prefix="rrp_test_")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_tmpdir}/test_platform.db"
os.environ["UPLOAD_DIR"] = f"{_tmpdir}/tenants"
os.environ["DEMO_DB_PATH"] = f"{_tmpdir}/demo_biz.db"
os.environ["SECRET_KEY"] = "test-secret-key-for-pytest"
os.environ["DEBUG"] = "false"

import pytest
from fastapi.testclient import TestClient

from backend.main import app
from backend.utils.security import create_access_token


@pytest.fixture(scope="module")
def client():
    """TestClient 上下文触发 lifespan（建表 + 种子用户）"""
    with TestClient(app) as c:
        yield c


def login(client, username, password):
    return client.post("/v1/auth/login", json={"username": username, "password": password})


# ---------- 登录 ----------

def test_login_success(client):
    """正确账号密码 → 200 + token"""
    r = login(client, "admin", "Admin@1234")
    assert r.status_code == 200
    data = r.json()
    assert data["access_token"]
    assert data["token_type"] == "bearer"
    assert data["user"]["username"] == "admin"


def test_login_wrong_password(client):
    """错误密码 → 401"""
    r = login(client, "admin", "wrong-password")
    assert r.status_code == 401


def test_login_unknown_user(client):
    """不存在用户 → 401"""
    r = login(client, "nobody", "whatever")
    assert r.status_code == 401


# ---------- 未认证 ----------

def test_no_token_401(client):
    """无 token 访问受保护接口 → 401"""
    assert client.get("/v1/tenants").status_code == 401
    assert client.get("/v1/tenants/T001/tasks").status_code == 401
    assert client.get("/v1/tenants/T001/regulations/documents").status_code == 401


def test_health_anonymous(client):
    """健康检查与登录保持匿名可访问"""
    assert client.get("/health").status_code == 200
    assert client.post("/v1/auth/login", json={}).status_code in (401, 422)  # 可访问，只是凭证错误


def test_forged_token_401(client):
    """伪造 token（错误密钥签名）→ 401"""
    from jose import jwt as jose_jwt
    from datetime import datetime, timedelta
    forged = jose_jwt.encode(
        {"sub": "fake", "username": "hacker", "exp": datetime.utcnow() + timedelta(hours=1)},
        "wrong-secret", algorithm="HS256",
    )
    r = client.get("/v1/tenants", headers={"Authorization": f"Bearer {forged}"})
    assert r.status_code == 401


def test_expired_token_401(client):
    """过期 token → 401（过期时间 -1 分钟签发）"""
    expired = create_access_token("whatever", "admin", expires_minutes=-1)["access_token"]
    r = client.get("/v1/tenants", headers={"Authorization": f"Bearer {expired}"})
    assert r.status_code == 401


# ---------- 租户越权 ----------

def test_me_tenants(client):
    """/auth/me 返回各自可访问租户"""
    admin_token = login(client, "admin", "Admin@1234").json()["access_token"]
    r = client.get("/v1/auth/me", headers={"Authorization": f"Bearer {admin_token}"})
    assert r.status_code == 200
    assert {t["id"] for t in r.json()["tenants"]} == {"T001", "T002"}

    zhangsan_token = login(client, "zhangsan", "Zhangsan@1234").json()["access_token"]
    r = client.get("/v1/auth/me", headers={"Authorization": f"Bearer {zhangsan_token}"})
    assert {t["id"] for t in r.json()["tenants"]} == {"T001"}


def test_cross_tenant_403(client):
    """zhangsan 仅绑 T001，访问 T002 → 403"""
    token = login(client, "zhangsan", "Zhangsan@1234").json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    assert client.get("/v1/tenants/T002", headers=headers).status_code == 403
    assert client.get("/v1/tenants/T002/tasks", headers=headers).status_code == 403
    assert client.get("/v1/tenants/T002/regulations/documents", headers=headers).status_code == 403


def test_list_tasks_no_longer_anonymous(client):
    """list_tasks 挂鉴权后：匿名 401、有权限 200"""
    assert client.get("/v1/tenants/T001/tasks").status_code == 401
    token = login(client, "zhangsan", "Zhangsan@1234").json()["access_token"]
    r = client.get("/v1/tenants/T001/tasks", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert "tasks" in r.json()


def test_admin_can_access_both_tenants(client):
    """admin 绑两个租户，均可访问"""
    token = login(client, "admin", "Admin@1234").json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    assert client.get("/v1/tenants/T001", headers=headers).status_code == 200
    assert client.get("/v1/tenants/T002", headers=headers).status_code == 200
