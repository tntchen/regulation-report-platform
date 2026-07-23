"""
安全工具纯函数单测（L2-Day10 补齐）
覆盖：密码哈希/校验、JWT 签发/解析往返、过期 token、伪造 token（错误密钥/篡改载荷）、
get_jwt_secret 三分支（环境注入 / debug 兜底 / 非 debug 缺失报错）。

运行方式: python -m pytest tests/test_security_unit.py -v
"""

import os
import tempfile

# 与其他测试一致的隔离环境（settings 在 import 时读取环境变量）
_tmpdir = tempfile.mkdtemp(prefix="rrp_test_sec_")
os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{_tmpdir}/test_platform.db")
os.environ.setdefault("UPLOAD_DIR", f"{_tmpdir}/tenants")
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-pytest")
os.environ.setdefault("DEBUG", "false")

import pytest
from jose import jwt as jose_jwt

from backend.config import settings
from backend.utils.security import (
    create_access_token, decode_access_token, get_jwt_secret,
    hash_password, verify_password, JWTError, JWT_ALGORITHM,
    _DEV_FALLBACK_SECRET,
)


# ============================================
# 密码哈希
# ============================================

class TestPassword:
    def test_hash_and_verify(self):
        hashed = hash_password("S3cret@123")
        assert hashed != "S3cret@123"
        assert verify_password("S3cret@123", hashed) is True

    def test_wrong_password_rejected(self):
        hashed = hash_password("S3cret@123")
        assert verify_password("wrong-password", hashed) is False

    def test_hash_salt_randomized(self):
        """同一明文两次哈希结果不同（bcrypt 随机盐）"""
        assert hash_password("same") != hash_password("same")


# ============================================
# JWT 签发 / 解析
# ============================================

class TestJWT:
    def test_roundtrip(self):
        """签发后解析应还原 sub/username，且 expires_in 与分钟数一致"""
        out = create_access_token("u001", "admin", expires_minutes=30)
        assert out["expires_in"] == 1800
        payload = decode_access_token(out["access_token"])
        assert payload["sub"] == "u001"
        assert payload["username"] == "admin"
        assert "exp" in payload and "iat" in payload

    def test_default_expire_from_settings(self):
        out = create_access_token("u001", "admin")
        assert out["expires_in"] == settings.access_token_expire_minutes * 60

    def test_expired_token_rejected(self):
        """过期 token 解析抛 JWTError"""
        out = create_access_token("u001", "admin", expires_minutes=-1)
        with pytest.raises(JWTError):
            decode_access_token(out["access_token"])

    def test_forged_wrong_secret_rejected(self):
        """攻击者用错误密钥签发的 token 必须拒绝"""
        forged = jose_jwt.encode(
            {"sub": "u001", "username": "admin"},
            "attacker-secret", algorithm=JWT_ALGORITHM,
        )
        with pytest.raises(JWTError):
            decode_access_token(forged)

    def test_tampered_payload_rejected(self):
        """签发后篡改载荷（改 sub 提权）签名验证失败"""
        out = create_access_token("u001", "admin")
        # 用 jose 的 get_unverified_claims 取载荷再改 sub，错误密钥重签
        claims = jose_jwt.get_unverified_claims(out["access_token"])
        claims["sub"] = "u999-root"
        tampered = jose_jwt.encode(claims, "attacker-secret", algorithm=JWT_ALGORITHM)
        with pytest.raises(JWTError):
            decode_access_token(tampered)

    def test_malformed_token_rejected(self):
        with pytest.raises(JWTError):
            decode_access_token("not.a.jwt")
        with pytest.raises(JWTError):
            decode_access_token("")


# ============================================
# get_jwt_secret 分支
# ============================================

class TestSecretResolution:
    def test_env_secret_first(self, monkeypatch):
        monkeypatch.setattr(settings, "secret_key", "env-injected")
        monkeypatch.setattr(settings, "debug", False)
        assert get_jwt_secret() == "env-injected"

    def test_debug_fallback(self, monkeypatch):
        """debug 模式无环境密钥时用内置开发兜底"""
        monkeypatch.setattr(settings, "secret_key", "")
        monkeypatch.setattr(settings, "debug", True)
        assert get_jwt_secret() == _DEV_FALLBACK_SECRET

    def test_production_missing_secret_raises(self, monkeypatch):
        """非 debug 且无环境密钥 → 启动报错（安全评审要求）"""
        monkeypatch.setattr(settings, "secret_key", "")
        monkeypatch.setattr(settings, "debug", False)
        with pytest.raises(RuntimeError, match="SECRET_KEY"):
            get_jwt_secret()
