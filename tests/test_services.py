"""服务台账（issue #2 方案 C）测试：采集器 mock / db upsert / 聚合持久化 / API 契约。"""
import asyncio
import json
import time

import pytest

from app import config, constants, db
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


def test_upsert_preserves_manual_row(tmp_path):
    """manual 行不被自动采集覆盖：同键 upsert auto/open 后 source/enabled 保持原值。"""
    db.close()
    db.init_db(str(tmp_path / "t.db"))
    base = {"device_id": "dev-a", "device_name": "a", "tailscale_ip": "100.64.0.9",
            "port": 10003, "auto_name": "manual-svc", "status": "open",
            "first_seen_ts": 100.0, "last_seen_ts": 100.0, "updated_ts": 100.0}
    db.upsert_services("cloud-1", [
        {**base, "source": "manual", "enabled": 0},
        {**base, "port": 10004, "source": "auto", "enabled": 1},
    ])
    # 采集器开放路径：source=auto、enabled 落到默认 1
    db.upsert_services("cloud-1", [
        {**base, "source": "auto", "enabled": 1,
         "last_seen_ts": 200.0, "updated_ts": 200.0},
        {**base, "port": 10004, "source": "auto", "enabled": 1,
         "last_seen_ts": 200.0, "updated_ts": 200.0},
    ])
    by_port = {r["port"]: r for r in db.get_services()}
    assert by_port[10003]["source"] == "manual"
    assert by_port[10003]["enabled"] == 0
    assert by_port[10003]["last_seen_ts"] == 200.0   # 其余列照常刷新
    # 对照：auto 行行为不变
    assert by_port[10004]["source"] == "auto"
    assert by_port[10004]["enabled"] == 1
    assert by_port[10004]["last_seen_ts"] == 200.0
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
    """服务 closed → 下线告警；恢复 open → 告警自动 resolved。

    端口用约定区内 + 老服务（first_seen 早于存活门槛），符合告警语义；
    约定区外/新发现的 closed 行为见 test_services_lifecycle_*。
    """
    from app import constants
    from app.collectors.base import CollectorResult
    from app.services.aggregator import Aggregator
    db.close()
    db.init_db(str(tmp_path / "t.db"))
    agg = Aggregator()

    def result(status: str) -> CollectorResult:
        import time as _t
        now = _t.time()
        r = CollectorResult(node_id="cloud-1", group=constants.GROUP_SERVICES)
        r.payload = {"services": [{
            "device_id": "dev-a", "device_name": "a", "tailscale_ip": "100.64.0.9",
            "port": 10001, "protocol": "http", "auto_name": "panel",
            "status": status, "missing_rounds": 0, "source": "auto",
            "first_seen_ts": 100.0,                 # 老服务（过存活门槛）
            "last_seen_ts": now, "updated_ts": now,  # 新近 closed（不触发 prune）
        }]}
        return r

    # 1) closed → 产生告警（source=service:100.64.0.9:10001）
    _run(agg.ingest(result("closed")))
    alerts = db.get_alerts(unresolved_only=True)
    assert len(alerts) == 1
    assert alerts[0]["source"] == "service:100.64.0.9:10001"
    assert alerts[0]["level"] == constants.LEVEL_WARN
    assert "下线" in alerts[0]["message"]

    # 2) open → resolved
    _run(agg.ingest(result("open")))
    assert db.get_alerts(unresolved_only=True) == []
    db.close()


# ================= 行生命周期与告警降噪（2026-08-24 审查修复） =================

def _svc_agg(tmp_path):
    from app.collectors.base import CollectorResult
    from app.services.aggregator import Aggregator
    db.close()
    db.init_db(str(tmp_path / "t.db"))
    return Aggregator(), CollectorResult


def test_alert_active_source_dedup(tmp_path):
    """同 source 持续故障只保持一条未解决行（曾 7 天堆 310 条）。"""
    db.close()
    db.init_db(str(tmp_path / "t.db"))
    assert db.insert_alert("warn", "service:1.2.3.4:8899", "下线") is True
    assert db.insert_alert("warn", "service:1.2.3.4:8899", "下线（冷却到期）") is False
    assert db.count_unresolved_alerts() == 1          # 不新插
    # 恢复后可再次告警（新的一条 active）
    db.resolve_alerts("service:1.2.3.4:8899")
    assert db.insert_alert("warn", "service:1.2.3.4:8899", "再次下线") is True
    assert db.count_unresolved_alerts() == 1
    db.close()


def test_services_prune_stale_extra_row(tmp_path):
    """约定区外且不在 extra 配置的行（历史登记残留）→ 删行 + 告警 resolve。"""
    agg, CollectorResult = _svc_agg(tmp_path)
    db.upsert_services("cloud-1", [
        {"device_id": "6b86b273", "device_name": "cloud", "tailscale_ip": "100.64.0.1",
         "port": 8899, "protocol": "http", "auto_name": "FRP Monitor",
         "status": "closed", "source": "auto", "first_seen_ts": 1.0,
         "last_seen_ts": 1.0, "updated_ts": 1.0}])
    db.insert_alert("warn", "service:100.64.0.1:8899", "服务下线")
    r = CollectorResult(node_id="cloud-1", group="services", payload={"services": []})
    _run(agg.ingest(r))
    assert db.get_services() == []                    # 残留行被删
    assert db.count_unresolved_alerts() == 0          # 历史误报一并 resolve
    db.close()


def test_services_prune_ephemeral_closed(tmp_path):
    """约定区内 closed 且距 last_seen 超 PRUNE_CLOSED_S（手机 ephemeral）→ 删。

    关键：updated_ts 每轮探测都刷新（新值），但 last_seen_ts 停留在最后在线
    ——prune 必须看 last_seen_ts，否则永不触发。
    """
    from app import config
    agg, CollectorResult = _svc_agg(tmp_path)
    import time as _t
    stale = _t.time() - config.SERVICES_PRUNE_CLOSED_S - 60
    db.upsert_services("cloud-1", [
        {"device_id": "ts-phone", "device_name": "phone", "tailscale_ip": "100.64.0.3",
         "port": 15577, "protocol": "tcp", "auto_name": "phone:15577",
         "status": "closed", "source": "auto", "first_seen_ts": stale,
         "last_seen_ts": stale, "updated_ts": _t.time()}])   # updated 新鲜也删
    db.insert_alert("warn", "service:100.64.0.3:15577", "下线")
    _run(agg.ingest(CollectorResult(node_id="cloud-1", group="services",
                                    payload={"services": []})))
    assert db.get_services() == []
    assert db.count_unresolved_alerts() == 0
    db.close()


def test_services_recent_closed_not_pruned(tmp_path):
    """约定区内 closed 但 last_seen 距今 < PRUNE_CLOSED_S（新近下线）→ 保留告警。"""
    agg, CollectorResult = _svc_agg(tmp_path)
    import time as _t
    now = _t.time()
    r = CollectorResult(node_id="cloud-1", group="services")
    r.payload = {"services": [{
        "device_id": "dev-a", "device_name": "a", "tailscale_ip": "100.64.0.9",
        "port": 10002, "protocol": "http", "auto_name": "recent-down",
        "status": "closed", "missing_rounds": 3, "source": "auto",
        "first_seen_ts": now - 86400, "last_seen_ts": now - 120,
        "updated_ts": now}]}
    _run(agg.ingest(r))
    assert db.count_unresolved_alerts() == 1     # 新近下线正常告警
    assert len(db.get_services()) == 1           # 行保留
    db.close()


def test_alert_startup_dedup(tmp_path):
    """启动压缩：同 source 多条未解决历史堆积 → 只留最新一条。"""
    db.close()
    db.init_db(str(tmp_path / "t.db"))
    # 直接用底层连接堆 6 条（模拟历史存量——insert 去重上线前的堆积形态）
    import sqlite3 as _s
    raw = _s.connect(str(tmp_path / "t.db"))
    for i in range(6):
        raw.execute(
            "INSERT INTO alerts(ts,level,source,message,resolved) VALUES (?,?,?, ?,0)",
            (1000.0 + i, "warn", "service:100.64.0.2:10155", f"下线 #{i}"))
    raw.commit()
    raw.close()
    assert db.count_unresolved_alerts() == 6
    assert db.dedup_unresolved_alerts() == 5
    assert db.count_unresolved_alerts() == 1
    db.close()


def test_services_manual_row_not_pruned(tmp_path):
    """manual 人工登记行永不 prune（即使长期 closed）。"""
    agg, CollectorResult = _svc_agg(tmp_path)
    db.upsert_services("cloud-1", [
        {"device_id": "local", "device_name": "localhost", "tailscale_ip": "127.0.0.1",
         "port": 12345, "protocol": "tcp", "auto_name": "人工登记",
         "status": "closed", "source": "manual", "first_seen_ts": 1.0,
         "last_seen_ts": 1.0, "updated_ts": 1.0}])
    _run(agg.ingest(CollectorResult(node_id="cloud-1", group="services",
                                    payload={"services": []})))
    rows = db.get_services()
    assert len(rows) == 1 and rows[0]["source"] == "manual"
    db.close()


def test_services_offline_device_suppression(tmp_path):
    """设备离线（休眠）时其服务 closed 不告警（tailscale ping 先例）。"""
    agg, CollectorResult = _svc_agg(tmp_path)
    db.upsert_devices("cloud-1", [
        {"device_id": "ts-desktop", "name": "desktop", "tailscale_ip": "100.64.0.2",
         "online": False, "status": "offline"}])
    r = CollectorResult(node_id="cloud-1", group="services")
    r.payload = {"services": [{
        "device_id": "ts-desktop", "device_name": "desktop", "tailscale_ip": "100.64.0.2",
        "port": 10002, "protocol": "http", "auto_name": "svc",
        "status": "closed", "missing_rounds": 3, "source": "auto",
        "first_seen_ts": 100.0, "last_seen_ts": 100.0}]}
    _run(agg.ingest(r))
    assert db.count_unresolved_alerts() == 0          # 休眠期间静默
    db.close()


def test_services_min_age_suppression(tmp_path):
    """首见 < SERVICES_ALERT_MIN_AGE_S 的临时端口下线不告警。"""
    agg, CollectorResult = _svc_agg(tmp_path)
    import time as _t
    r = CollectorResult(node_id="cloud-1", group="services")
    r.payload = {"services": [{
        "device_id": "ts-phone", "device_name": "phone", "tailscale_ip": "100.64.0.3",
        "port": 15577, "protocol": "tcp", "auto_name": "phone:15577",
        "status": "closed", "missing_rounds": 3, "source": "auto",
        "first_seen_ts": _t.time() - 60,        # 1 分钟前才发现
        "last_seen_ts": _t.time() - 30}]}
    _run(agg.ingest(r))
    assert db.count_unresolved_alerts() == 0
    db.close()


def test_scope_file_ports_are_merged(tmp_path, monkeypatch):
    """terraform probe_scope.json 的 public_tcp 并入探测集合（含端口段展开）。"""
    from app.collectors.services import ServicesCollector
    scope = {
        "servers": {
            "cloud": {"host": "124.221.190.237",
                      "public_tcp": ["22", "443", "20000-20002"]},
        }
    }
    f = tmp_path / "probe_scope.json"
    f.write_text(json.dumps(scope), encoding="utf-8")
    monkeypatch.setattr(config, "SERVICES_SCOPE_FILE", str(f))
    col = ServicesCollector()
    assert col._scope_ports("124.221.190.237") == [22, 443, 20000, 20001, 20002]
    assert col._scope_ports("10.0.0.9") == []
    # 文件 mtime 未变时走缓存；改变内容后应重新加载
    f.write_text(json.dumps({"servers": {"x": {"host": "124.221.190.237",
                                               "public_tcp": ["8080"]}}}),
                 encoding="utf-8")
    import os
    os.utime(f, (time.time() + 5, time.time() + 5))
    assert col._scope_ports("124.221.190.237") == [8080]
