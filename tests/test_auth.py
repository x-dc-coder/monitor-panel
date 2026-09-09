"""SSO 认证与鉴权中间件测试：白名单、登录拦截、会话维持、单点登出。"""
import pytest
from starlette.testclient import TestClient
from app import config
from app.main import app


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
