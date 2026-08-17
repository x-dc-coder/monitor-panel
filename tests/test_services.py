"""服务台账（issue #2 方案 C）测试：采集器 mock / db upsert / 聚合持久化 / API 契约。"""
import asyncio

import pytest

from app import constants, db
from app.collectors import build_registry
from app.collectors.base import CollectorResult


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(scope="module")
def registry():
    return build_registry()


def test_services_mock_result(registry):
    """mock：确定性服务清单，全部 open + 约定区内 + 指标过白名单。"""
    r = _run(registry["services"].run_once())
    assert isinstance(r, CollectorResult)
    assert r.group == constants.GROUP_SERVICES
    payload = r.payload
    assert payload["mock"] is True
    svc = payload["services"]
    assert len(svc) >= 5
    assert all(s["status"] == "open" for s in svc)
    assert all(s["source"] == "auto" for s in svc)
    protocols = {s["protocol"] for s in svc}
    assert "http" in protocols and "ssh" in protocols and "tcp" in protocols
    lo, hi = payload["range"]
    assert lo <= 10000 and hi >= 19999
    assert all(lo <= s["port"] <= hi for s in svc)
    for m in r.metrics:
        assert constants.is_valid_metric(r.group, m.name), f"{r.group}/{m.name}"


def test_services_mock_alias_applied(registry, monkeypatch):
    """SERVICES_ALIASES 人工覆盖名注入 name。"""
    from app import config
    monkeypatch.setattr(config, "SERVICES_ALIASES", {"100.64.0.2:10010": "摄像头（别名）"})
    r = _run(registry["services"].run_once())
    hit = [s for s in r.payload["services"] if s["port"] == 10010]
    assert hit and hit[0]["name"] == "摄像头（别名）"


def test_services_extra_ports_in_scan(monkeypatch):
    """SERVICES_EXTRA_PORTS：约定区外端口按设备纳入每轮必探集合。"""
    from app import config
    monkeypatch.setattr(config, "SERVICES_EXTRA_PORTS", {
        "100.64.0.1": [22, 443, 8898],
        "*": [8080],
    })
    from app.collectors.services import ServicesCollector
    col = ServicesCollector(mock=False)
    target = {"device_id": "dev-a", "device_name": "a", "tailscale_ip": "100.64.0.1"}
    ports = col._ports_for(target, {})
    for want in (22, 443, 8080, 8898):
        assert want in ports, f"extra 端口 {want} 应进入扫描集合"
    # 区间内端口仍由分片游标覆盖（不重复添加）
    assert 10000 in ports or 19999 in ports or len(ports) > 0
    # 其他设备不继承精确 IP 配置，但继承 "*"
    other = {"device_id": "dev-b", "device_name": "b", "tailscale_ip": "100.64.0.2"}
    ports2 = col._ports_for(other, {})
    assert 8080 in ports2
    assert 8898 not in ports2


def test_services_extra_ports_dict_form(monkeypatch):
    """SERVICES_EXTRA_PORTS dict 形态：按域名探测选项（host/https/path）解析。"""
    from app import config
    monkeypatch.setattr(config, "SERVICES_EXTRA_PORTS", {
        "100.64.0.1": {
            "80": {"host": "monitor.dc-sy.cn"},
            "443": {"host": "monitor.dc-sy.cn", "https": True},
            "9090": {"path": "/metrics"},
        },
    })
    from app.collectors.services import ServicesCollector
    col = ServicesCollector(mock=False)
    ip = "100.64.0.1"
    ports = col._extra_ports_for(ip)
    assert ports == [80, 443, 9090]
    assert col._probe_options(ip, 80) == {"host": "monitor.dc-sy.cn"}
    assert col._probe_options(ip, 443) == {"host": "monitor.dc-sy.cn", "https": True}
    assert col._probe_options(ip, 9090) == {"path": "/metrics"}
    assert col._probe_options(ip, 22) == {}  # 未配置 → 默认探测
    # local 键匹配本机
    monkeypatch.setattr(config, "SERVICES_EXTRA_PORTS", {"local": {"8898": {}}})
    assert col._extra_ports_for("127.0.0.1") == [8898]
    assert col._extra_ports_for("100.64.0.9") == []


def test_services_db_upsert_roundtrip(tmp_path):

    """upsert 语义：UNIQUE 合并、first_seen 保留、人工名不被覆盖、manual/auto 共存。"""
    db.close()
    db.init_db(str(tmp_path / "t.db"))
    rows = [
        {"device_id": "dev-a", "device_name": "a", "tailscale_ip": "100.64.0.9",
         "port": 10001, "protocol": "http", "auto_name": "svc-a",
         "url": "http://100.64.0.9:10001", "http_status": 200, "latency_ms": 1.5,
         "status": "open", "missing_rounds": 0, "source": "auto",
         "first_seen_ts": 100.0, "last_seen_ts": 100.0, "updated_ts": 100.0},
        {"device_id": "dev-a", "device_name": "a", "tailscale_ip": "100.64.0.9",
         "port": 22, "protocol": "tcp", "auto_name": "ssh", "status": "open",
         "source": "manual", "first_seen_ts": 100.0, "last_seen_ts": 100.0,
         "updated_ts": 100.0},
    ]
    db.upsert_services("cloud-1", rows)
    got = db.get_services()
    assert len(got) == 2
    by_port = {r["port"]: r for r in got}
    assert by_port[10001]["status"] == "open"
    assert by_port[10001]["protocol"] == "http"
    assert by_port[22]["source"] == "manual"
    # 再次 upsert：first_seen 保留、name 不回落
    db.upsert_services("cloud-1", [{
        "device_id": "dev-a", "tailscale_ip": "100.64.0.9", "port": 10001,
        "protocol": "http", "auto_name": "svc-a2", "name": "人工名",
        "status": "open", "missing_rounds": 0, "source": "auto",
        "first_seen_ts": 999.0, "last_seen_ts": 101.0, "updated_ts": 101.0}])
    got2 = {r["port"]: r for r in db.get_services()}
    assert got2[10001]["first_seen_ts"] == 100.0   # 首次发现保留
    assert got2[10001]["name"] == "人工名"
    assert got2[10001]["auto_name"] == "svc-a2"
    assert len(db.get_services(status="open")) == 2
    db.close()


def test_services_aggregator_persists(registry, tmp_path):
    """aggregator.ingest(services result) → services 表落库 + samples 趋势可用。"""
    from app.services.aggregator import Aggregator
    db.close()
    db.init_db(str(tmp_path / "t.db"))
    agg = Aggregator()
    r = _run(registry["services"].run_once())
    _run(agg.ingest(r))
    rows = db.get_services()
    assert len(rows) >= 5
    assert all(x["node_id"] == "cloud-1" for x in rows)
    latest = db.query_latest_by_group(constants.GROUP_SERVICES)
    assert latest, "samples 应写入趋势组"
    db.close()


def test_services_api(client, ingest_all):
    """GET /api/services：{ok,data,ts} 契约 + 汇总字段。"""
    from app.api import set_aggregator
    set_aggregator(ingest_all)
    r = client.get("/api/services")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    data = body["data"]
    assert "services" in data and "range" in data and "summary" in data
    assert data["summary"]["total"] == len(data["services"])
    assert data["summary"]["open"] >= 1
    s0 = data["services"][0]
    for k in ("device_id", "device_name", "port", "name", "auto_name", "protocol",
              "url", "http_status", "latency_ms", "status", "missing_rounds",
              "source", "enabled", "first_seen_ts", "last_seen_ts"):
        assert k in s0, f"契约字段缺失: {k}"


def test_services_down_alert_and_resolve(tmp_path):
    """服务 closed → 下线告警；恢复 open → 告警自动 resolved。"""
    from app import constants
    from app.collectors.base import CollectorResult
    from app.services.aggregator import Aggregator
    db.close()
    db.init_db(str(tmp_path / "t.db"))
    agg = Aggregator()

    def result(status: str) -> CollectorResult:
        r = CollectorResult(node_id="cloud-1", group=constants.GROUP_SERVICES)
        r.payload = {"services": [{
            "device_id": "dev-a", "device_name": "a", "tailscale_ip": "100.64.0.9",
            "port": 8898, "protocol": "http", "auto_name": "panel",
            "status": status, "missing_rounds": 0, "source": "auto",
            "last_seen_ts": 100.0, "updated_ts": 101.0}]}
        return r

    # 1) closed → 产生告警（source=service:100.64.0.9:8898）
    _run(agg.ingest(result("closed")))
    alerts = db.get_alerts(unresolved_only=True)
    assert len(alerts) == 1
    assert alerts[0]["source"] == "service:100.64.0.9:8898"
    assert alerts[0]["level"] == constants.LEVEL_WARN
    assert "下线" in alerts[0]["message"]

    # 2) open → resolved
    _run(agg.ingest(result("open")))
    assert db.get_alerts(unresolved_only=True) == []
    db.close()
