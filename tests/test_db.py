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
