"""SQLite 层测试：白名单/批量写入/降采样/upsert/告警冷却/清理。"""
import time

import pytest

from app import db


@pytest.fixture(autouse=True)
def _fresh_db(tmp_path):
    db.close()
    db.init_db(tmp_path / "test.db")
    yield
    db.close()


def test_insert_samples_whitelist():
    ts = time.time()
    ok = db.insert_samples("cloud-1", "system", [
        ("cpu_percent", 12.3, ts),
        ("mem_percent", 41.2, ts),
        ("disk_percent./data", 53.0, ts),       # 合法 suffix
    ])
    assert ok == 3
    skipped = db.insert_samples("cloud-1", "system", [
        ("evil_metric", 1.0, ts),                # 不合法指标名
        ("cpu_percent", 1.0, ts),
    ])
    assert skipped == 1
    # 不合法 group 整体拒绝
    assert db.insert_samples("cloud-1", "bogus_group", [("x", 1.0, ts)]) == 0
    latest = db.query_latest_by_group("system")
    assert latest["cpu_percent"] == 1.0  # 白名单外的被过滤


def test_history_downsample():
    ts = time.time() - 100
    metrics = [("cpu_percent", float(i % 10), ts + i) for i in range(20)]
    db.insert_samples("cloud-1", "system", metrics)
    # ts..ts+19，since=ts-1，桶 10s → 桶 0/1/2 共 3 个
    points = db.query_history("cpu_percent", ts - 1, bucket_s=10.0, group="system")
    assert len(points) == 3
    assert all("value" in p and "ts" in p for p in points)


def test_query_history_node_filtering():
    ts = time.time()
    db.insert_samples("cloud-1", "system", [("cpu_percent", 10.0, ts)])
    db.insert_samples("cloud-2", "system", [("cpu_percent", 50.0, ts)])

    p_default = db.query_history("cpu_percent", ts - 1, bucket_s=10.0, group="system")
    assert len(p_default) == 1
    assert p_default[0]["value"] == 30.0  # node_id=None 时全部聚合

    p_c1 = db.query_history("cpu_percent", ts - 1, bucket_s=10.0, group="system", node_id="cloud-1")
    assert len(p_c1) == 1
    assert p_c1[0]["value"] == 10.0

    p_c2 = db.query_history("cpu_percent", ts - 1, bucket_s=10.0, group="system", node_id="cloud-2")
    assert len(p_c2) == 1
    assert p_c2[0]["value"] == 50.0


def test_upsert_devices_and_tunnels():
    db.upsert_devices("cloud-1", [{"device_id": "d1", "name": "dev1", "online": 1}])
    db.upsert_devices("cloud-1", [{"device_id": "d1", "name": "dev1b", "online": 0}])
    rows = db.get_devices("cloud-1")
    assert len(rows) == 1  # upsert 不重复
    assert rows[0]["name"] == "dev1b"
    db.upsert_tunnels("cloud-1", [{"tunnel_id": "t1", "status": "online"}])
    assert len(db.get_tunnels("cloud-1")) == 1


def test_alert_cooldown_and_resolve():
    assert db.insert_alert("warn", "src:1", "msg") is True
    assert db.insert_alert("warn", "src:1", "msg", cooldown_s=3600) is False  # 冷却
    assert db.insert_alert("warn", "src:2", "msg") is True                     # 不同 source
    assert db.count_unresolved_alerts() == 2
    db.resolve_alerts("src:1")
    assert db.count_unresolved_alerts() == 1
    # 冷却窗口过后可再插
    db.insert_alert("warn", "src:1", "msg", cooldown_s=0)
    assert db.count_unresolved_alerts() == 2


def test_cleanup_removes_old():
    old_sample = time.time() - 40 * 86400       # 40 天前（samples 保留 30 天 → 删）
    old_alert = time.time() - 100 * 86400       # 100 天前（alerts 保留 90 天 → 删）
    db.insert_samples("cloud-1", "system", [("cpu_percent", 1.0, old_sample)])
    db.insert_samples("cloud-1", "system", [("cpu_percent", 2.0, time.time())])
    db.insert_alert("warn", "src:old", "old alert")
    conn = db._cursor()
    with db._lock:
        conn.execute("UPDATE alerts SET ts=? WHERE source=?", (old_alert, "src:old"))
        conn.commit()
    res = db.cleanup(now=time.time() + 3600)  # 推进时钟
    assert res["samples"] == 1
    assert res["alerts"] == 1
    _, _, n = db.query_sample_range("cpu_percent")
    assert n == 1


def test_cleanup_prunes_stale_devices():
    """cleanup 移除 updated_ts 超 7 天的设备残留行（重注册/退网旧行）。"""
    from app import db as app_db
    now = time.time()
    app_db.upsert_devices("cloud-1", [
        {"device_id": "fresh", "name": "fresh-node", "online": True},
        {"device_id": "ghost", "name": "ghost-node", "online": False},
    ])
    conn = app_db._cursor()
    with app_db._lock:
        conn.execute("UPDATE devices SET updated_ts=? WHERE device_id='ghost'",
                     (now - 8 * 86400,))
        conn.commit()
    app_db.cleanup(now)
    ids = {r["device_id"] for r in app_db.get_devices()}
    assert "fresh" in ids and "ghost" not in ids


def test_cleanup_prunes_stale_devices():
    """cleanup 移除 updated_ts 超 7 天的设备残留行（重注册/退网旧行）。"""
    from app import db as app_db
    now = time.time()
    app_db.upsert_devices("cloud-1", [
        {"device_id": "fresh", "name": "fresh-node", "online": True},
        {"device_id": "ghost", "name": "ghost-node", "online": False},
    ])
    conn = app_db._cursor()
    with app_db._lock:
        conn.execute("UPDATE devices SET updated_ts=? WHERE device_id='ghost'",
                     (now - 8 * 86400,))
        conn.commit()
    app_db.cleanup(now)
    ids = {r["device_id"] for r in app_db.get_devices()}
    assert "fresh" in ids and "ghost" not in ids


def test_query_latest_metrics_picks_newest():
    ts = time.time()
    db.insert_samples("cloud-1", "system", [("cpu_percent", 10.0, ts)])
    db.insert_samples("cloud-1", "system", [("cpu_percent", 20.0, ts + 1)])
    db.insert_samples("cloud-1", "system", [("cpu_percent", 30.0, ts + 2)])
    latest = db.query_latest_metrics("system", ["cpu_percent"])
    assert latest == {"cpu_percent": 30.0}


def test_query_latest_metrics_suffixed_name():
    ts = time.time()
    db.insert_samples("cloud-1", "ts_latency", [
        ("latency_ms.oneplus13", 18.5, ts),
        ("latency_ms.oneplus13", 21.0, ts + 1),
        ("latency_ms.laptop", 3.0, ts),
    ])
    latest = db.query_latest_metrics("ts_latency", ["latency_ms.oneplus13"])
    assert latest == {"latency_ms.oneplus13": 21.0}


def test_query_latest_metrics_node_filtering():
    ts = time.time()
    db.insert_samples("cloud-1", "system", [("cpu_percent", 10.0, ts)])
    db.insert_samples("cloud-2", "system", [("cpu_percent", 90.0, ts + 1)])
    assert db.query_latest_metrics("system", ["cpu_percent"], node_id="cloud-1") == {
        "cpu_percent": 10.0}
    assert db.query_latest_metrics("system", ["cpu_percent"], node_id="cloud-2") == {
        "cpu_percent": 90.0}
    # 无 node 过滤时取全库最新
    assert db.query_latest_metrics("system", ["cpu_percent"]) == {"cpu_percent": 90.0}


def test_query_latest_metrics_empty():
    assert db.query_latest_metrics("system", ["cpu_percent", "mem_percent"]) == {}
    assert db.query_latest_metrics("system", []) == {}
