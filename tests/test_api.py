"""API 冒烟测试（mock 数据源）：契约 {ok, data, ts} + 边界。"""


def _seed(client, ingest_all):
    """把预置聚合层注入 API（client 已启动后调用，顺序确定）。"""
    from app.api import set_aggregator
    set_aggregator(ingest_all)
    return ingest_all


def test_healthz(client):
    r = client.get("/api/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["mock"] is True
    assert "collectors" in body


def test_overview_and_endpoints(client, ingest_all):
    _seed(client, ingest_all)
    r = client.get("/api/overview")
    assert r.status_code == 200
    d = r.json()["data"]
    assert d["frp"]["total"] == 3 and d["frp"]["online"] == 3
    assert d["tailscale"]["total"] == 4
    assert d["certs"]["total"] == 4
    assert d["certs"]["crit"] == 1
    assert "system" in d and "alerts" in d

    r = client.get("/api/devices")
    assert r.status_code == 200
    devices = r.json()["data"]["devices"]
    assert len(devices) == 4
    mapped = {x["name"]: x["mapped_8899"] for x in devices}
    assert mapped.get("oppo-pad-mini") == "laptop"

    r = client.get("/api/frp")
    assert r.status_code == 200
    frp = r.json()["data"]
    assert len(frp["tunnels"]) == 3
    assert len(frp["link"]) == 3  # 8899 三设备链路质量

    for ep in ("/api/system", "/api/certs", "/api/network"):
        assert client.get(ep).status_code == 200

    r = client.get("/api/alerts")
    assert r.status_code == 200
    levels = {a["level"] for a in r.json()["data"]["alerts"]}
    assert "warn" in levels and "crit" in levels  # 阈值引擎已触发


def test_history_api(client, ingest_all):
    _seed(client, ingest_all)
    r = client.get("/api/history", params={"metric": "cpu_percent", "group": "system", "range": "1h"})
    assert r.status_code == 200
    d = r.json()["data"]
    assert d["metric"] == "cpu_percent" and d["count"] >= 1
    assert client.get("/api/history", params={"metric": "evil"}).status_code == 400
    assert client.get("/api/history", params={"metric": "cpu_percent", "range": "99d"}).status_code == 400


def test_manual_ping_and_rate_limit(client, ingest_all):
    _seed(client, ingest_all)
    r = client.post("/api/devices/ts-dc-desktop/ping")
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["latency_ms"] is not None and data["mock"] is True
    r2 = client.post("/api/devices/ts-dc-desktop/ping")
    assert r2.status_code == 429  # 限流 5s/设备（评审 E8）
    assert client.post("/api/devices/nope/ping").status_code == 404


def test_ingest_disabled_404(client):
    r = client.post("/api/ingest", json={"results": []})
    assert r.status_code == 404  # 二期默认禁用（评审 E10）


def test_index_page_and_assets(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "monitor.dc-sy.cn" in r.text
    for asset in ("/dist/app.js", "/style.css"):
        assert client.get(asset).status_code == 200
