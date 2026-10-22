"""接口契约测试：统一错误信封 + openapi 200 schema 覆盖。"""
import pytest

from app import config


@pytest.fixture()
def seeded(client, ingest_all):
    from app.api import set_aggregator
    set_aggregator(ingest_all)
    return ingest_all


def _assert_error_envelope(r, status: int) -> None:
    assert r.status_code == status, r.text
    body = r.json()
    assert set(body) == {"ok", "error", "ts"}, body
    assert body["ok"] is False
    assert isinstance(body["error"], str) and body["error"]
    assert isinstance(body["ts"], float)


def test_error_envelope_uniform(client, seeded, monkeypatch):
    """错误一律 {ok:false, error:<str>, ts}，状态码不变。"""
    # ping 404
    _assert_error_envelope(client.post("/api/devices/nope/ping"), 404)

    # ping 429（同一设备连续两次）
    device_id = client.get("/api/devices").json()["data"]["devices"][0]["device_id"]
    assert client.post(f"/api/devices/{device_id}/ping").status_code == 200
    _assert_error_envelope(client.post(f"/api/devices/{device_id}/ping"), 429)

    # history 400（未知 metric）
    _assert_error_envelope(
        client.get("/api/history", params={"metric": "evil"}), 400)

    # history 422（缺少必填 query 参数）
    _assert_error_envelope(client.get("/api/history"), 422)

    overrides = {
        "INGEST_ENABLED": True,
        "INGEST_TOKEN": "contract-token",
    }
    for k, v in overrides.items():
        monkeypatch.setattr(config, k, v)

    # ingest 400（未知 node_id）
    r = client.post("/api/ingest",
                    json={"results": [{"node_id": "ghost", "group": "system",
                                       "metrics": []}]},
                    headers={"Authorization": "Bearer contract-token"})
    _assert_error_envelope(r, 400)

    # ingest 401（错 token，保留 WWW-Authenticate）
    r = client.post("/api/ingest", json={"results": []},
                    headers={"Authorization": "Bearer wrong"})
    _assert_error_envelope(r, 401)
    assert r.headers["www-authenticate"] == "Bearer"

    # /api/me 401（未登录）
    _assert_error_envelope(client.get("/api/me"), 401)


def test_openapi_coverage():
    """除 healthz / ingest / auth/* 外，每个端点 200 必须引用具名 schema，
    且 4XX 必须引用统一 ErrorEnvelope。"""
    from app.main import app

    skip = {"/api/healthz", "/api/ingest"}
    for path, item in app.openapi()["paths"].items():
        if path in skip or path.startswith("/auth/"):
            continue
        for method, op in item.items():
            if method not in ("get", "post", "put", "patch", "delete"):
                continue
            responses = op.get("responses", {})
            schema = responses.get("200", {}).get(
                "content", {}).get("application/json", {}).get("schema", {})
            assert "$ref" in schema, f"{method.upper()} {path} 缺少具名 200 schema: {schema}"
            err_schema = responses.get("4XX", {}).get(
                "content", {}).get("application/json", {}).get("schema", {})
            ref = err_schema.get("$ref", "")
            assert ref.endswith("/ErrorEnvelope"), \
                f"{method.upper()} {path} 4XX 未引用 ErrorEnvelope: {err_schema}"


_ENVELOPE_ENDPOINTS = [
    ("/api/overview", {}),
    ("/api/devices", {}),
    ("/api/frp", {}),
    ("/api/grm", {}),
    ("/api/history", {"metric": "cpu_percent", "group": "system"}),
    ("/api/system", {}),
    ("/api/certs", {}),
    ("/api/network", {}),
    ("/api/services", {}),
    ("/api/updates", {}),
    ("/api/alerts", {}),
    ("/api/taishan", {}),
]


def test_success_envelope_shape(client, seeded):
    for path, params in _ENVELOPE_ENDPOINTS:
        r = client.get(path, params=params)
        assert r.status_code == 200, f"{path}: {r.text}"
        body = r.json()
        assert set(body) == {"ok", "data", "ts"}, f"{path}: {set(body)}"
        assert body["ok"] is True
        assert isinstance(body["ts"], float)


def test_envelope_ts_is_response_time(client, seeded):
    import time
    before = time.time()
    body = client.get("/api/overview").json()
    after = time.time()
    assert before <= body["ts"] <= after


def test_services_data_has_no_ts(client, seeded):
    from app import db
    db.upsert_services("cloud-1", [{"device_id": "ts-local", "port": 8080}])
    data = client.get("/api/services").json()["data"]
    assert "ts" not in data


def test_grm_data_has_no_ts(client, seeded):
    from app import db
    db.upsert_grm_repos("cloud-1", [{"repo_id": "local/repo"}])
    data = client.get("/api/grm").json()["data"]
    assert "ts" not in data


def test_updates_data_has_no_ts(client, seeded):
    from app import db
    db.upsert_update_items("cloud-1", [{"provider": "grm", "item_key": "k"}])
    data = client.get("/api/updates").json()["data"]
    assert "ts" not in data


def test_frp_snapshot_ts_preserved(client, seeded):
    data = client.get("/api/frp").json()["data"]
    assert isinstance(data["ts"], float)


def test_system_snapshot_ts_preserved(client, seeded):
    data = client.get("/api/system").json()["data"]
    assert isinstance(data["ts"], float)


def test_certs_snapshot_ts_preserved(client, seeded):
    data = client.get("/api/certs").json()["data"]
    assert isinstance(data["ts"], float)


def test_network_snapshot_ts_preserved(client, seeded):
    data = client.get("/api/network").json()["data"]
    assert isinstance(data["ts"], float)


def test_ping_mock_shape_superset(client, seeded):
    """mock ping 与真实 tailscale ping 同形状（hostname/latency_min_ms/ts）。"""
    from app.api import devices as devices_mod
    devices_mod._last_manual_ping.clear()
    device_id = client.get("/api/devices").json()["data"]["devices"][-1]["device_id"]
    data = client.post(f"/api/devices/{device_id}/ping").json()["data"]
    for key in ("device_id", "hostname", "tailscale_ip", "latency_ms",
                "latency_min_ms", "path", "loss_percent", "timed_out",
                "ts", "mock"):
        assert key in data, key
    assert isinstance(data["ts"], float)


def test_healthz_flat_shape_frozen(client):
    """deploy.sh 依赖的平铺形状：不得包信封，必须有 ok/mode。"""
    body = client.get("/api/healthz").json()
    assert "data" not in body
    assert body["ok"] is True
    assert body["mode"] in ("mock", "snapshot", "live")
    for key in ("uptime", "version", "mock", "node", "collectors"):
        assert key in body


def test_validation_error_message(client):
    r = client.get("/api/history")
    assert r.status_code == 422
    assert r.json()["error"].startswith("参数校验失败: query.metric")


def test_ingest_error_detail_string(client, monkeypatch):
    from app import config
    monkeypatch.setattr(config, "INGEST_ENABLED", True)
    monkeypatch.setattr(config, "INGEST_TOKEN", "t")
    r = client.post("/api/ingest",
                    json={"results": [{"node_id": "ghost", "group": "system",
                                       "metrics": []}]},
                    headers={"Authorization": "Bearer t"})
    err = r.json()["error"]
    assert isinstance(err, str)
    assert "ghost" in err and "registered" in err


def test_err_helper_removed():
    import app.api as api_mod
    assert not hasattr(api_mod, "err")


def test_openapi_refs_resolve():
    from app.main import app
    schema = app.openapi()
    components = schema["components"]["schemas"]
    for path, item in schema["paths"].items():
        if path in {"/api/healthz", "/api/ingest"} or path.startswith("/auth/"):
            continue
        for method, op in item.items():
            if method not in ("get", "post", "put", "patch", "delete"):
                continue
            ref = op["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
            name = ref.rsplit("/", 1)[-1]
            assert name in components, f"{method.upper()} {path}: {ref} 不存在"
