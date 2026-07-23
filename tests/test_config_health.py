"""
配置收口与深度健康检查测试（L2 Day 10）
覆盖：
- AI 适配器 fail-fast：非 mock 模式缺失 API Key 立即报错，不静默降级
- mock 模式仅显式开启时生效
- /health 分级返回（ok/degraded/down），响应结构向后兼容（保留 status/version）

运行方式: python -m pytest tests/test_config_health.py -v
"""

import os
import tempfile

# 在导入 app 之前切换到临时测试库 + 固定 JWT 密钥（与 test_auth 同一约定）
_tmpdir = tempfile.mkdtemp(prefix="rrp_test_cfg_")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_tmpdir}/test_platform.db"
os.environ["UPLOAD_DIR"] = f"{_tmpdir}/tenants"
os.environ["DEMO_DB_PATH"] = f"{_tmpdir}/demo_biz.db"
os.environ["SECRET_KEY"] = "test-secret-key-for-pytest"
os.environ["DEBUG"] = "false"
os.environ["AI_MOCK_MODE"] = "true"

import pytest
from fastapi.testclient import TestClient

from backend.config import settings
from backend.core.ai_adapter import AIAdapterFactory, AIBackendAdapter, MockAIAdapter
from backend.main import app


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


# ---------- AI fail-fast ----------

def test_mock_mode_returns_mock_adapter(monkeypatch):
    """ai_mock_mode=True 时返回 Mock 适配器（无需 API Key）"""
    monkeypatch.setattr(settings, "ai_mock_mode", True)
    monkeypatch.setattr(settings, "ai_api_key", "")
    adapter = AIAdapterFactory.get_adapter()
    assert isinstance(adapter, MockAIAdapter)


def test_missing_key_non_mock_fails_fast(monkeypatch):
    """非 mock 模式且未配置 API Key → 立即抛清晰错误，不静默降级"""
    monkeypatch.setattr(settings, "ai_mock_mode", False)
    monkeypatch.setattr(settings, "ai_api_key", "")
    with pytest.raises(RuntimeError, match="API Key"):
        AIAdapterFactory.get_adapter()


def test_placeholder_key_non_mock_fails_fast(monkeypatch):
    """占位符 Key（your-kimi-api-key）同样视为未配置 → fail-fast"""
    monkeypatch.setattr(settings, "ai_mock_mode", False)
    monkeypatch.setattr(settings, "ai_api_key", "your-kimi-api-key")
    with pytest.raises(RuntimeError, match="API Key"):
        AIAdapterFactory.get_adapter()


def test_valid_key_non_mock_returns_real_adapter(monkeypatch):
    """非 mock 模式且配置了有效 Key → 返回真实适配器"""
    monkeypatch.setattr(settings, "ai_mock_mode", False)
    monkeypatch.setattr(settings, "ai_api_key", "sk-real-test-key")
    adapter = AIAdapterFactory.get_adapter()
    assert isinstance(adapter, AIBackendAdapter)
    assert not isinstance(adapter, MockAIAdapter)


def test_tenant_scope_missing_key_fails_fast(monkeypatch):
    """租户维度缺 Key 同样 fail-fast，报错信息含租户标识"""
    monkeypatch.setattr(settings, "ai_mock_mode", False)
    monkeypatch.setattr(settings, "ai_backup_api_key", "")
    with pytest.raises(RuntimeError, match="T002"):
        AIAdapterFactory.get_adapter(tenant_id="T002")


# ---------- /health 深度健康检查 ----------

def test_health_returns_backward_compatible_structure(client):
    """/health 保留 status/version 字段，新增 checks 明细"""
    r = client.get("/health")
    assert r.status_code == 200
    data = r.json()
    assert "status" in data
    assert "version" in data
    assert data["status"] in ("ok", "degraded", "down")
    assert "checks" in data
    assert set(data["checks"]) == {"database", "vector_dir", "ai"}


def test_health_mock_mode_marks_ai_as_mock(client):
    """mock 模式下 AI 检查项标注为 mock（不假装真实连通），整体 status 仍为 ok"""
    r = client.get("/health")
    data = r.json()
    assert data["checks"]["ai"] == "mock"
    assert data["checks"]["database"] == "ok"
    assert data["checks"]["vector_dir"] == "ok"
    assert data["status"] == "ok"
