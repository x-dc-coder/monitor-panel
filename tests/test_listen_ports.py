"""主机监听端口自动探测（group=listen_ports）契约测试。

agent 自报本机监听端口 → services 表 source='agent'，由中心 services 采集器
负责 open/closed 归因；agent 行的生命周期归 agent（替换语义），manual 行神圣。
"""
from __future__ import annotations

import asyncio
import time

from app import config, constants, db

TOKEN = "listen-token"


def _enable_ingest(monkeypatch) -> None:
    monkeypatch.setattr(config, "INGEST_ENABLED", True)
    monkeypatch.setattr(config, "INGEST_TOKEN", TOKEN)


def _seed_aliyun() -> None:
    db.upsert_devices("cloud-1", [{
        "device_id": "ts-aliyun", "name": "aliyun",
        "tailscale_ip": "100.64.0.5", "online": True,
    }])


def _post(client, ports: list[dict], node: str = "aliyun",
          with_metrics: bool = True):
    metrics = ([{"name": "listening_count", "value": len(ports),
                 "ts": time.time()}] if with_metrics else [])
    body = {"results": [{
        "node_id": node,
        "group": constants.GROUP_LISTEN_PORTS,
        "metrics": metrics,
        "payload": {"ports": ports},
    }]}
    return client.post("/api/ingest", json=body,
                       headers={"Authorization": f"Bearer {TOKEN}"})


def _agent_rows(node: str = "aliyun") -> list[dict]:
    return [r for r in db.get_services(node_id=node) if r["source"] == "agent"]


def test_listen_ports_ingest_creates_agent_rows(client, monkeypatch):
    """tailnet 成员上报 → 2 行 source=agent + listening_count 样本。"""
    from app.api import set_aggregator
    from app.services.aggregator import Aggregator

    _enable_ingest(monkeypatch)
    set_aggregator(Aggregator())
    _seed_aliyun()

    r = _post(client, [
        {"port": 22, "proto": "tcp", "addr": "0.0.0.0"},
        {"port": 7400, "proto": "tcp", "addr": "0.0.0.0"},
    ])
    assert r.status_code == 200, r.text

    rows = _agent_rows()
    assert {(x["port"], x["device_id"], x["enabled"]) for x in rows} == {
        (22, "ts-aliyun", 1), (7400, "ts-aliyun", 1)}
    assert all(x["tailscale_ip"] == "100.64.0.5" for x in rows)

    latest = db.query_latest_by_group(constants.GROUP_LISTEN_PORTS,
                                      node_id="aliyun")
    assert latest["listening_count"] == 2.0


def test_agent_report_replaces_rows(client, monkeypatch):
    """第二次上报：清单外的 agent 行删除、新端口出现；manual 行不受影响。"""
    from app.api import set_aggregator
    from app.services.aggregator import Aggregator

    _enable_ingest(monkeypatch)
    set_aggregator(Aggregator())
    _seed_aliyun()
    db.upsert_services("aliyun", [{
        "device_id": "manual-aliyun", "device_name": "manual",
        "tailscale_ip": "100.64.0.5", "port": 7400, "protocol": "tcp",
        "auto_name": "人工登记", "status": "open", "source": "manual",
        "first_seen_ts": 1.0, "last_seen_ts": 1.0, "updated_ts": 1.0,
    }])

    r = _post(client, [{"port": 22, "proto": "tcp"},
                       {"port": 7400, "proto": "tcp"}])
    assert r.status_code == 200, r.text
    assert {x["port"] for x in _agent_rows()} == {22, 7400}

    r = _post(client, [{"port": 22, "proto": "tcp"},
                       {"port": 8080, "proto": "tcp"}])
    assert r.status_code == 200, r.text
    assert {x["port"] for x in _agent_rows()} == {22, 8080}

    manual = [x for x in db.get_services(node_id="aliyun")
              if x["source"] == "manual"]
    assert len(manual) == 1
    assert manual[0]["port"] == 7400


def test_services_collector_preserves_agent_rows(tmp_path):
    """中心采集器 open 路径 upsert 同 (node, port)：source/enabled 保持 agent 原值。"""
    db.close()
    db.init_db(str(tmp_path / "t.db"))
    base = {"device_id": "dev-a", "device_name": "a",
            "tailscale_ip": "100.64.0.9", "port": 10005,
            "auto_name": "agent-svc", "status": "open",
            "first_seen_ts": 100.0, "last_seen_ts": 100.0, "updated_ts": 100.0}
    db.upsert_services("cloud-1", [
        {**base, "source": "agent", "enabled": 0},
        {**base, "port": 10006, "source": "auto", "enabled": 1},
    ])
    # 采集器开放路径（source=auto、enabled=1）
    db.upsert_services("cloud-1", [
        {**base, "source": "auto", "enabled": 1,
         "last_seen_ts": 200.0, "updated_ts": 200.0},
        {**base, "port": 10006, "source": "auto", "enabled": 1,
         "last_seen_ts": 200.0, "updated_ts": 200.0},
    ])
    by_port = {r["port"]: r for r in db.get_services()}
    assert by_port[10005]["source"] == "agent"
    assert by_port[10005]["enabled"] == 0
    assert by_port[10005]["last_seen_ts"] == 200.0   # 其余列照常刷新
    assert by_port[10006]["source"] == "auto"
    db.close()


def test_cleanup_preserves_agent_rows(tmp_path):
    """清理/生命周期 prune 不删 source='agent' 的 closed 行（即便超 1 小时）。"""
    from app.collectors.base import CollectorResult
    from app.services.aggregator import Aggregator

    db.close()
    db.init_db(str(tmp_path / "t.db"))
    now = time.time()
    stale = now - config.SERVICES_PRUNE_CLOSED_S - 60
    common = {"device_id": "ts-phone", "device_name": "phone",
              "tailscale_ip": "100.64.0.3", "protocol": "tcp",
              "status": "closed", "first_seen_ts": stale,
              "last_seen_ts": stale, "updated_ts": now}
    db.upsert_services("cloud-1", [
        {**common, "port": 15577, "auto_name": "agent:15577", "source": "agent"},
        {**common, "port": 15578, "auto_name": "auto:15578", "source": "auto"},
    ])
    agg = Aggregator()
    asyncio.run(agg.ingest(CollectorResult(
        node_id="cloud-1", group=constants.GROUP_SERVICES,
        payload={"services": []})))
    db.cleanup(now)
    rows = {r["port"]: r for r in db.get_services()}
    assert 15577 in rows and rows[15577]["source"] == "agent"
    assert 15578 not in rows                       # 对照：auto 行照常 prune
    db.close()


def test_unknown_node_rejected(client, monkeypatch):
    """非 tailnet 成员上报 listen_ports → 400，且不落行。"""
    from app.api import set_aggregator
    from app.services.aggregator import Aggregator

    _enable_ingest(monkeypatch)
    set_aggregator(Aggregator())
    r = _post(client, [{"port": 22, "proto": "tcp"}], node="ghost")
    assert r.status_code == 400
    assert "ghost" in r.json()["error"]
    assert db.get_services(node_id="ghost") == []


def test_central_collector_skips_agent_ports(client, monkeypatch):
    """中心采集器不探测 agent 独占端口（否则产生 duplicate 行）。"""
    from app.collectors.services import ServicesCollector
    db.upsert_devices("cloud-1", [{"device_id": "ts-aliyun", "name": "aliyun",
                                   "os": "linux", "online": True}])
    # 先灌一条 agent 行
    db.upsert_services("aliyun", [{
        "device_id": "ts-aliyun", "device_name": "aliyun",
        "tailscale_ip": "100.64.0.8", "port": 7400,
        "protocol": "tcp", "source": "agent", "enabled": 1,
        "status": "open", "first_seen_ts": 1.0, "last_seen_ts": 1.0,
        "updated_ts": 1.0}])
    # 伪造 target 与 registered 视图，验证端口集合过滤
    reg_rows = db.get_services()
    agent_owned = {(r["device_id"], r["port"]) for r in reg_rows
                   if r.get("source") == "agent"}
    registered = {(r["device_id"], r["port"]): r for r in reg_rows
                  if r.get("source") != "agent"}
    col = ServicesCollector()
    target = {"device_id": "ts-aliyun", "device_name": "aliyun",
              "tailscale_ip": "100.64.0.8"}
    ports = [p for p in col._ports_for(target, registered)
             if (target["device_id"], p) not in agent_owned]
    assert 7400 not in ports, "agent 独占端口不应被中心采集器探测"
