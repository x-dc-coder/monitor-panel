"""issue #9 Phase 1 契约测试：TaiShan 出网质量（白名单/端点/ingest/告警/趋势）。

全真 SQLite 零 mock；数据唯一来源是 dc-pi 的 POST /api/ingest 上报
（与 lab-monitor 转发器同模式）。
"""
import time

import pytest

from app import config, constants, db
from app.api import set_aggregator
from app.collectors.base import CollectorResult
from app.services.aggregator import Aggregator

_TS = 1_700_000_000.0

_PAYLOAD = {
    "active_node": "hk-01",
    "state": "healthy",
    "summary": "hk-01 三阶段全通，加权延迟 182ms",
    "qualified_count": 3,
    "top_candidates": [
        {"name": "hk-01", "score": 182.0, "base": 40.0, "openai": 220.0, "gemini": 260.0},
        {"name": "jp-02", "score": 205.5, "base": 55.0, "openai": 240.0, "gemini": 300.0},
    ],
    "switched": False,
    "reason": "",
    "ts": _TS,
}


def _metrics(qualified: float = 3, ts: float = _TS) -> list[dict]:
    return [
        {"name": "score", "value": 182.0, "ts": ts},
        {"name": "latency_base_ms", "value": 40.0, "ts": ts},
        {"name": "latency_openai_ms", "value": 220.0, "ts": ts},
        {"name": "latency_gemini_ms", "value": 260.0, "ts": ts},
        {"name": "qualified_count", "value": qualified, "ts": ts},
    ]


def _seed_taishan(agg: Aggregator, node_id: str, payload: dict,
                  qualified: float = 3, ts: float | None = None) -> None:
    r = CollectorResult(node_id=node_id, group=constants.GROUP_TAISHAN)
    for m in _metrics(qualified, ts or _TS):
        r.add(m["name"], m["value"], m["ts"])
    r.payload = payload
    agg._ingest_blocking(r, ts or _TS)


@pytest.fixture(autouse=True)
def clean_taishan_state(client):
    """taishan 相关表按用例清空（lifespan 会重连到共享测试库，行会跨用例累积）。"""
    conn = db._cursor()
    with db._lock:
        conn.execute("DELETE FROM samples WHERE metric_group=?", (constants.GROUP_TAISHAN,))
        conn.execute("DELETE FROM taishan_snapshot")
        conn.execute("DELETE FROM alerts WHERE source LIKE 'taishan:%'")
        conn.commit()
    yield
    conn = db._cursor()
    with db._lock:
        conn.execute("DELETE FROM samples WHERE metric_group=?", (constants.GROUP_TAISHAN,))
        conn.execute("DELETE FROM taishan_snapshot")
        conn.execute("DELETE FROM alerts WHERE source LIKE 'taishan:%'")
        conn.commit()


@pytest.fixture()
def ingest_headers(monkeypatch, client):
    """开 ingest 并让 dc-pi 成为已知节点（devices 表命中 = tailnet 成员，自动登记）。"""
    monkeypatch.setattr(config, "INGEST_ENABLED", True)
    monkeypatch.setattr(config, "INGEST_TOKEN", "taishan-token")
    db.upsert_devices("cloud-1", [{"device_id": "ts-dc-pi", "name": "dc-pi",
                                   "os": "linux", "online": True}])
    conn = db._cursor()
    with db._lock:
        conn.execute("DELETE FROM monitor_nodes WHERE id='dc-pi'")
        conn.commit()
    assert not db.node_exists("dc-pi")
    return {"Authorization": "Bearer taishan-token"}


@pytest.fixture()
def taishan_agg(client):
    """client 已启动（lifespan 注册了自己的聚合层），此处覆盖为可控实例。"""
    agg = Aggregator()
    set_aggregator(agg)
    return agg


def test_taishan_whitelist():
    """5 个基础名全部合法；"taishan.score" 点号形态被拒（点号是实例后缀语义）。"""
    for name in ("score", "latency_base_ms", "latency_openai_ms",
                 "latency_gemini_ms", "qualified_count"):
        assert constants.is_valid_metric(constants.GROUP_TAISHAN, name) is True, name
    assert constants.is_valid_metric(constants.GROUP_TAISHAN, "taishan.score") is False


def test_taishan_envelope_shape(client, taishan_agg):
    """信封三字段 + TaishanData 字段类型；空态（全 None/[]）合法。"""
    r = client.get("/api/taishan")
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"ok", "data", "ts"}
    assert body["ok"] is True
    assert isinstance(body["ts"], float)
    data = body["data"]
    assert set(data) == {"active_node", "state", "summary", "qualified_count",
                         "top_candidates", "switched", "reason", "ts"}
    assert data["active_node"] is None
    assert data["state"] is None
    assert data["summary"] is None
    assert data["qualified_count"] is None
    assert data["top_candidates"] == []
    assert data["switched"] is None
    assert data["reason"] is None
    assert data["ts"] is None


def test_taishan_node_filter(client, taishan_agg):
    """两节点快照隔离：?node=dc-pi 取 dc-pi，无参按 config.NODE_ID 回退快照表。"""
    payload_pi = {**_PAYLOAD, "active_node": "hk-01", "qualified_count": 3}
    payload_cloud = {**_PAYLOAD, "active_node": "jp-02", "qualified_count": 1}
    _seed_taishan(taishan_agg, "dc-pi", payload_pi, qualified=3)
    _seed_taishan(taishan_agg, "cloud-1", payload_cloud, qualified=1)

    r = client.get("/api/taishan", params={"node": "dc-pi"})
    assert r.status_code == 200
    assert r.json()["data"]["active_node"] == "hk-01"
    assert r.json()["data"]["qualified_count"] == 3

    r_def = client.get("/api/taishan")
    assert r_def.status_code == 200
    assert r_def.json()["data"]["active_node"] == "jp-02"

    # overview 聚合区块与 /api/taishan 同源
    block = client.get("/api/overview").json()["data"]["taishan"]
    assert block["active_node"] == "jp-02"
    assert block["qualified_count"] == 1
    assert block["ts"] == _TS


def test_taishan_ingest_e2e(client, taishan_agg, ingest_headers):
    """POST /api/ingest 五指标 + payload → 200 accepted=1、samples 5 行、latest_for 非空。"""
    body = {"results": [{
        "node_id": "dc-pi",
        "group": constants.GROUP_TAISHAN,
        "metrics": _metrics(),
        "payload": _PAYLOAD,
    }]}
    r = client.post("/api/ingest", json=body, headers=ingest_headers)
    assert r.status_code == 200, r.text
    assert r.json()["data"]["accepted"] == 1

    conn = db._cursor()
    with db._lock:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM samples WHERE metric_group=? AND node_id=?",
            (constants.GROUP_TAISHAN, "dc-pi")).fetchone()
    assert row["n"] == 5

    latest = taishan_agg.latest_for(constants.GROUP_TAISHAN, "dc-pi")
    assert latest["active_node"] == "hk-01"

    read = client.get("/api/taishan", params={"node": "dc-pi"}).json()["data"]
    assert read["active_node"] == "hk-01"
    assert read["top_candidates"][0]["name"] == "hk-01"


def test_taishan_snapshot_persistence(client, taishan_agg, ingest_headers):
    """ingest 落快照表；新 Aggregator 实例（模拟重启）后端点仍返回快照内容。"""
    body = {"results": [{
        "node_id": "dc-pi",
        "group": constants.GROUP_TAISHAN,
        "metrics": _metrics(),
        "payload": _PAYLOAD,
    }]}
    r = client.post("/api/ingest", json=body, headers=ingest_headers)
    assert r.status_code == 200, r.text

    snap = db.get_taishan_snapshot("dc-pi")
    assert snap is not None
    assert snap["active_node"] == "hk-01"
    assert snap["qualified_count"] == 3
    assert snap["ts"] == _TS

    # 模拟重启：内存快照清空
    set_aggregator(Aggregator())
    read = client.get("/api/taishan", params={"node": "dc-pi"}).json()["data"]
    assert read["active_node"] == "hk-01"
    assert read["summary"] == _PAYLOAD["summary"]
    assert read["ts"] == _TS


def test_taishan_alert_all_failed(client, taishan_agg):
    """qualified_count=0 → crit 告警 source=taishan:proxy；恢复后全部 resolved。"""
    _seed_taishan(taishan_agg, "dc-pi", {**_PAYLOAD, "state": "all_failed"}, qualified=0)
    unresolved = db.get_alerts(limit=200, unresolved_only=True)
    hit = [a for a in unresolved if a["source"] == "taishan:proxy"]
    assert len(hit) == 1
    assert hit[0]["level"] == constants.LEVEL_CRIT

    _seed_taishan(taishan_agg, "dc-pi", _PAYLOAD, qualified=3, ts=_TS + 60)
    assert not [a for a in db.get_alerts(limit=200, unresolved_only=True)
                if a["source"] == "taishan:proxy"]


def test_taishan_no_alert_on_normal(client, taishan_agg):
    """正常上报不产生 taishan: 告警。"""
    _seed_taishan(taishan_agg, "dc-pi", _PAYLOAD, qualified=3)
    sources = {a["source"] for a in db.get_alerts(limit=200)}
    assert not {s for s in sources if s.startswith("taishan:")}


def test_taishan_events_dropped(client, taishan_agg, ingest_headers):
    """请求体带 events 字段 → 200 且 alerts 无新增（events 被静默丢弃的契约守卫）。"""
    before = len(db.get_alerts(limit=500))
    body = {"results": [{
        "node_id": "dc-pi",
        "group": constants.GROUP_TAISHAN,
        "metrics": _metrics(),
        "payload": _PAYLOAD,
        "events": [{"type": "switch", "message": "不应入库"}],
    }]}
    r = client.post("/api/ingest", json=body, headers=ingest_headers)
    assert r.status_code == 200, r.text
    assert len(db.get_alerts(limit=500)) == before


def test_taishan_history(client, taishan_agg):
    """趋势通道：/api/history?metric=latency_base_ms&group=taishan 有点。"""
    now = time.time()
    db.insert_samples("cloud-1", constants.GROUP_TAISHAN,
                      [("latency_base_ms", 40.0, now)])
    r = client.get("/api/history", params={"metric": "latency_base_ms",
                                           "group": constants.GROUP_TAISHAN,
                                           "range": "24h"})
    assert r.status_code == 200, r.text
    points = r.json()["data"]["points"]
    assert len(points) >= 1


def test_taishan_stale_watchdog(client, taishan_agg):
    """停报看门狗：有历史上报且最新 ts 超 90min → warn；恢复后 resolve；冷启动不告警。"""
    # 冷启动：表无行时不得误报
    taishan_agg.check_taishan_stale()
    assert not [a for a in db.get_alerts(limit=200) if a["source"] == "taishan:stale"]

    _seed_taishan(taishan_agg, "dc-pi", _PAYLOAD, qualified=3)
    # 内存时间戳拨回 2 小时前（模拟长时间未上报）
    taishan_agg.latest_ts[("dc-pi", constants.GROUP_TAISHAN)] = time.time() - 7200

    taishan_agg.check_taishan_stale()
    hit = [a for a in db.get_alerts(limit=200, unresolved_only=True)
           if a["source"] == "taishan:stale"]
    assert len(hit) == 1
    assert hit[0]["level"] == constants.LEVEL_WARN

    taishan_agg.latest_ts[("dc-pi", constants.GROUP_TAISHAN)] = time.time()
    taishan_agg.check_taishan_stale()
    assert not [a for a in db.get_alerts(limit=200, unresolved_only=True)
                if a["source"] == "taishan:stale"]
