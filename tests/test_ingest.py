"""issue #5：/api/ingest 应用层 token 认证三态（404 未启用 / 401 无或错 token / 200 正确）。

nginx 侧已对 location /api/ingest auth_basic off（setup_nginx_cert.sh），
豁免后本接口的应用层 Bearer token 是唯一防线。
"""
import pytest

from app import config


def _post(client, body=None, token=None):
    headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
    return client.post("/api/ingest", json=body or {}, headers=headers)


def test_disabled_returns_404(client):
    """INGEST_ENABLED=0（默认）：接口不可发现。"""
    assert config.INGEST_ENABLED is False
    r = _post(client, token="anything")
    assert r.status_code == 404


def test_enabled_without_token_returns_401(client, monkeypatch):
    monkeypatch.setattr(config, "INGEST_ENABLED", True)
    monkeypatch.setattr(config, "INGEST_TOKEN", "secret-token")
    r = _post(client, body={"results": []})
    assert r.status_code == 401


def test_enabled_wrong_token_returns_401(client, monkeypatch):
    monkeypatch.setattr(config, "INGEST_ENABLED", True)
    monkeypatch.setattr(config, "INGEST_TOKEN", "secret-token")
    r = _post(client, body={"results": []}, token="wrong-token")
    assert r.status_code == 401
    assert "invalid token" in r.json()["error"]


def test_enabled_correct_token_accepts(client, monkeypatch):
    monkeypatch.setattr(config, "INGEST_ENABLED", True)
    monkeypatch.setattr(config, "INGEST_TOKEN", "secret-token")
    from app import db
    db.seed_nodes([{"id": "cloud-2", "name": "cloud-2", "role": "secondary"}])
    r = _post(client, body={"results": [
        {"node_id": "cloud-2", "group": "system",
         "metrics": [{"name": "cpu_pct", "value": 12.5, "ts": 1700000000.0}]},
    ]}, token="secret-token")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["data"]["accepted"] == 1


def test_enabled_correct_token_rejects_bad_body(client, monkeypatch):
    monkeypatch.setattr(config, "INGEST_ENABLED", True)
    monkeypatch.setattr(config, "INGEST_TOKEN", "secret-token")
    r = _post(client, body={"unexpected": 1}, token="secret-token")
    assert r.status_code == 400


def test_ingest_empty_token_bearer_rejected(client, monkeypatch):
    """启用但 token 为空时，空 Bearer 头与无头都必须 401（不得匹配 "Bearer "）。"""
    monkeypatch.setattr(config, "INGEST_ENABLED", True)
    monkeypatch.setattr(config, "INGEST_TOKEN", "")

    r = _post(client, body={"results": []}, token="")
    assert r.status_code == 401
    assert "not configured" in r.json()["error"]

    r = _post(client, body={"results": []})
    assert r.status_code == 401
    assert "not configured" in r.json()["error"]


def test_validate_config_ingest_requires_token(monkeypatch):
    """live 模式下 INGEST_ENABLED=1 必须配置 token。"""
    monkeypatch.setattr(config, "MOCK", False)
    monkeypatch.setattr(config, "SNAPSHOT", False)
    monkeypatch.setattr(config, "FRPS_PASS", "frps-pass")
    monkeypatch.setattr(config, "LEGACY_TOKEN", "legacy-token")
    monkeypatch.setattr(config, "SSO_ENABLED", False)
    monkeypatch.setattr(config, "INGEST_ENABLED", True)
    monkeypatch.setattr(config, "INGEST_TOKEN", "")

    errors = config.validate_config()
    assert errors
    assert any("MONITOR_INGEST_TOKEN" in e for e in errors)
