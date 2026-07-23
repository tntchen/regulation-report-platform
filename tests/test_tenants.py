"""
租户动态化测试（FastAPI TestClient）
覆盖：种子脚本灌库、创建租户后创建者立即可用、非成员访问 403、
     更新配置即时生效（缓存失效）、非 admin 写操作 403

运行方式: python -m pytest tests/test_tenants.py -v
注意: 与其他测试共用同一 settings 单例模式（先导入者生效），断言做容差处理
"""

import os
import tempfile

# 在导入 app 之前切换到临时测试库 + 固定 JWT 密钥（与 test_auth.py 同模式）
_tmpdir = tempfile.mkdtemp(prefix="rrp_test_tenants_")
os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{_tmpdir}/test_platform.db")
os.environ.setdefault("UPLOAD_DIR", f"{_tmpdir}/tenants")
os.environ.setdefault("DEMO_DB_PATH", f"{_tmpdir}/demo_biz.db")
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


def auth_headers(client, username="admin", password="Admin@1234"):
    token = client.post("/v1/auth/login",
                        json={"username": username, "password": password}
                        ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def test_seed_preset_tenants(client):
    """种子加载：seed_preset_tenants 幂等灌库，列表 API 能读到 T001/T002"""
    import asyncio
    from backend.services.tenant_service import seed_preset_tenants

    created = asyncio.run(seed_preset_tenants())
    assert set(created) <= {"T001", "T002"}
    # 幂等：第二次运行不再写入
    assert asyncio.run(seed_preset_tenants()) == []

    headers = auth_headers(client)
    r = client.get("/v1/tenants", headers=headers)
    assert r.status_code == 200
    ids = {t["id"] for t in r.json()["tenants"]}
    assert {"T001", "T002"} <= ids  # admin 绑定 T001+T002
    # zhangsan 只能看到 T001（成员过滤）
    z_headers = auth_headers(client, "zhangsan", "Zhangsan@1234")
    z_ids = {t["id"] for t in client.get("/v1/tenants", headers=z_headers).json()["tenants"]}
    assert z_ids == {"T001"}


def test_create_tenant_then_usable(client):
    """admin 创建租户（带数据源与 AI 后端）→ 创建者立即可访问详情"""
    headers = auth_headers(client)
    payload = {
        "id": "T900",
        "name": "测试新租户",
        "code": "TEST_NEW",
        "ai_backend": {"provider": "local", "model": "test-model"},
        "data_sources": [{
            "source_id": "DS900", "source_name": "测试库",
            "db_type": "sqlite_demo", "readonly": True,
            "whitelist_tables": ["loan_contract"],
        }],
    }
    r = client.post("/v1/tenants/", headers=headers, json=payload)
    assert r.status_code == 200
    assert r.json()["id"] == "T900"

    # 重复创建 → 409
    assert client.post("/v1/tenants/", headers=headers, json=payload).status_code == 409

    # 创建者自动绑定为成员：详情接口立即可用，且配置与创建时一致
    r = client.get("/v1/tenants/T900", headers=headers)
    assert r.status_code == 200
    assert r.json()["name"] == "测试新租户"
    assert r.json()["ai_backend"]["model"] == "test-model"
    assert r.json()["data_sources"][0]["source_id"] == "DS900"


def test_create_tenant_non_admin_403(client):
    """非 admin 角色创建/更新租户 → 403"""
    z_headers = auth_headers(client, "zhangsan", "Zhangsan@1234")
    r = client.post("/v1/tenants/", headers=z_headers,
                    json={"id": "T901", "name": "x", "code": "X901"})
    assert r.status_code == 403
    r = client.put("/v1/tenants/T001", headers=z_headers, json={"name": "越权改名"})
    assert r.status_code == 403


def test_non_member_403(client):
    """非成员访问租户详情 → 403（zhangsan 不是 T002 成员）"""
    z_headers = auth_headers(client, "zhangsan", "Zhangsan@1234")
    assert client.get("/v1/tenants/T002", headers=z_headers).status_code == 403
    # 未认证 → 401
    assert client.get("/v1/tenants/T001").status_code == 401


def test_update_tenant_takes_effect(client):
    """更新租户配置 → 详情接口立即读到新值（缓存失效），且写审计"""
    headers = auth_headers(client)
    r = client.put("/v1/tenants/T900", headers=headers, json={
        "name": "测试新租户-改",
        "ai_backend": {"provider": "kimi", "model": "updated-model"},
    })
    assert r.status_code == 200
    assert r.json()["name"] == "测试新租户-改"

    r = client.get("/v1/tenants/T900", headers=headers)
    assert r.json()["name"] == "测试新租户-改"
    assert r.json()["ai_backend"]["model"] == "updated-model"

    # 更新不存在的租户 → 404
    r = client.put("/v1/tenants/T999", headers=headers, json={"name": "x"})
    assert r.status_code == 404

    # 审计落库：tenant.create / tenant.update 均可查到
    r = client.get("/v1/tenants/T900/audit-logs?action=tenant.update",
                   headers=headers)
    assert r.status_code == 200
    assert any(l["resource"] == "T900" for l in r.json()["logs"])
