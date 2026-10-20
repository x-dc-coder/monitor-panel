"""SSO 认证与鉴权中间件测试：白名单、登录拦截、会话维持、单点登出。"""
import json
from base64 import b64encode

import itsdangerous
import pytest
from starlette.middleware import Middleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.testclient import TestClient
from app import config
from app.main import app


def _sign_session(secret: str, data: dict) -> str:
    """按 starlette SessionMiddleware 的格式签名会话 cookie。"""
    signer = itsdangerous.TimestampSigner(secret)
    raw = b64encode(json.dumps(data).encode("utf-8"))
    return signer.sign(raw).decode("utf-8")


def _live_baseline(monkeypatch):
    """把环境切到 live 且数据源凭据齐全，便于单独断言会话密钥校验。"""
    monkeypatch.setattr(config, "MOCK", False)
    monkeypatch.setattr(config, "SNAPSHOT", False)
    monkeypatch.setattr(config, "FRPS_PASS", "frps-pass")
    monkeypatch.setattr(config, "LEGACY_TOKEN", "legacy-token")


def test_sso_bypass_when_not_configured(client):
    """未配置 CASDOOR_CLIENT_ID 时，中间件应旁路放行所有请求。"""
    config.CASDOOR_CLIENT_ID = ""
    r = client.get("/api/healthz")
    assert r.status_code == 200
    assert r.json()["ok"] is True

    r = client.get("/api/me")
    assert r.status_code == 200
    assert r.json()["ok"] is False  # 无 user session 但请求不被 401 拦截


def test_sso_whitelist_endpoints(client, monkeypatch):
    """启用 SSO 时，白名单端点（healthz、ingest、auth、静态资源）必须放行。"""
    monkeypatch.setattr(config, "CASDOOR_CLIENT_ID", "test-client-id")
    monkeypatch.setattr(config, "SSO_ENABLED", True)

    # 1. healthz 放行
    r = client.get("/api/healthz")
    assert r.status_code == 200

    # 2. ingest 放行（走自身的 token 认证逻辑，而不是被重定向到 /auth/login）
    r = client.post("/api/ingest", json={})
    # 无论 ingest 自身是 401/404 还是 200，它绝不应返回 HTML 302 重定向
    assert r.status_code in (401, 404, 422)

    # 3. 静态资源放行
    r = client.get("/dist/app.js")
    assert r.status_code in (200, 404)  # 即使 404 也不能被 302 拦截


def test_sso_guard_intercepts_unauthenticated(client, monkeypatch):
    """启用 SSO 时，未登录的页面访问应 302/307 重定向，API 应返回 401。"""
    monkeypatch.setattr(config, "CASDOOR_CLIENT_ID", "test-client-id")
    monkeypatch.setattr(config, "SSO_ENABLED", True)

    # API 请求拦截为 401 JSON
    r = client.get("/api/overview")
    assert r.status_code == 401
    assert r.json()["error"] == "unauthorized"

    # 页面请求拦截为重定向至 /auth/login
    r = client.get("/", follow_redirects=False)
    assert r.status_code in (302, 307)
    assert r.headers["location"] == "/auth/login"


def test_sso_logout(client, monkeypatch):
    """登出应走 Casdoor RP-initiated logout（/api/logout + post_logout_redirect_uri 自动跳转）。"""
    monkeypatch.setattr(config, "CASDOOR_ISSUER", "https://sso.dc-sy.cn")
    monkeypatch.setattr(config, "CASDOOR_CLIENT_ID", "test-client-id")
    r = client.get("/auth/logout", follow_redirects=False)
    assert r.status_code in (302, 307)
    loc = r.headers["location"]
    assert loc.startswith("https://sso.dc-sy.cn/api/logout?client_id=test-client-id")
    # post_logout_redirect_uri 回当前站点登录页（testserver 环境回退 base_url）
    assert "post_logout_redirect_uri=" in loc
    assert "testserver%2Fauth%2Flogin" in loc


def test_validate_config_default_session_secret_rejected(monkeypatch):
    """live + SSO 启用时，默认会话密钥必须触发启动校验错误。"""
    _live_baseline(monkeypatch)
    monkeypatch.setattr(config, "SSO_ENABLED", True)
    monkeypatch.setattr(config, "CASDOOR_CLIENT_ID", "test-client-id")
    monkeypatch.setattr(config, "SESSION_SECRET_KEY",
                        "monitor-panel-session-secret-key-change-me")

    errors = config.validate_config()
    assert errors
    assert any("MONITOR_SESSION_SECRET" in e for e in errors)

    # 换成 ≥32 字符随机密钥后该错误消失
    monkeypatch.setattr(config, "SESSION_SECRET_KEY", "a" * 64)
    assert not any("MONITOR_SESSION_SECRET" in e
                   for e in config.validate_config())


def test_forged_default_secret_cookie_rejected(client, monkeypatch):
    """生产使用随机密钥时，用默认密钥伪造的会话 cookie 不得通过鉴权。"""
    monkeypatch.setattr(config, "SSO_ENABLED", True)
    monkeypatch.setattr(config, "CASDOOR_CLIENT_ID", "test-client-id")
    # 重建 SessionMiddleware，模拟生产环境已配置随机密钥
    original = app.user_middleware[0]
    assert original.cls is SessionMiddleware
    patched = [
        Middleware(SessionMiddleware,
                   **{**original.kwargs, "secret_key": "f" * 64}),
        *app.user_middleware[1:],
    ]
    monkeypatch.setattr(app, "user_middleware", patched)
    monkeypatch.setattr(app, "middleware_stack",
                        app.build_middleware_stack())

    forged = _sign_session(
        "monitor-panel-session-secret-key-change-me",
        {"user": {"name": "attacker"}})
    r = client.get("/api/overview",
                   headers={"Cookie": f"monitor_session={forged}"})
    assert r.status_code == 401
    assert r.json()["error"] == "unauthorized"


def test_suffix_bypass_rejected(client, monkeypatch):
    """后缀不再放行：未认证 /api/foo.js 必须 401；真实产物路径放行不重定向。"""
    monkeypatch.setattr(config, "CASDOOR_CLIENT_ID", "test-client-id")
    monkeypatch.setattr(config, "SSO_ENABLED", True)

    r = client.get("/api/foo.js", follow_redirects=False)
    assert r.status_code == 401
    assert r.json()["error"] == "unauthorized"

    # web/index.html 引用的实际产物（web/dist/app.js、style.css、favicon.svg）
    for asset in ("/dist/app.js", "/style.css", "/favicon.svg"):
        r = client.get(asset, follow_redirects=False)
        assert r.status_code == 200, f"{asset} 应放行，实得 {r.status_code}"
