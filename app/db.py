"""SQLite(WAL) 存储层。

- PRAGMA：journal_mode=WAL + synchronous=NORMAL + busy_timeout=5000（评审 E12）
- 5 张表见 README §3.1 / 技术方案 §3.5；均带 node_id 支持多云
- samples 写入白名单校验（constants.is_valid_metric）+ 批量事务
- 清理：每小时 DELETE 过期 + wal_checkpoint(TRUNCATE)（低频低竞争）
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path

from app import config, constants

log = logging.getLogger(__name__)

_conn: sqlite3.Connection | None = None
_db_path: Path | None = None
# RLock：init_db → seed_nodes 存在嵌套获取（同线程重入），必须可重入
_lock = threading.RLock()  # 单连接串行化（单 worker，操作均为毫秒级）

SCHEMA = """
CREATE TABLE IF NOT EXISTS monitor_nodes (
  id          TEXT PRIMARY KEY,
  name        TEXT NOT NULL,
  role        TEXT DEFAULT 'primary',
  public_ip   TEXT,
  enabled     INTEGER DEFAULT 1,
  last_seen_ts REAL
);

CREATE TABLE IF NOT EXISTS devices (
  node_id      TEXT NOT NULL,
  device_id    TEXT NOT NULL,
  name         TEXT,
  tailscale_ip TEXT,
  os           TEXT,
  online       INTEGER,
  relay        TEXT,
  last_seen_ts REAL,
  rx_bytes     INTEGER,
  tx_bytes     INTEGER,
  updated_ts   REAL,
  PRIMARY KEY (node_id, device_id)
);

CREATE TABLE IF NOT EXISTS tunnels (
  node_id       TEXT NOT NULL,
  tunnel_id     TEXT NOT NULL,
  type          TEXT,
  remote_port   INTEGER,
  local_addr    TEXT,
  status        TEXT,
  traffic_in    INTEGER,
  traffic_out   INTEGER,
  updated_ts    REAL,
  PRIMARY KEY (node_id, tunnel_id)
);

CREATE TABLE IF NOT EXISTS samples (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  node_id      TEXT NOT NULL,
  metric_group TEXT NOT NULL,
  metric_name  TEXT NOT NULL,
  value        REAL NOT NULL,
  ts           REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_samples_lookup
  ON samples(metric_group, metric_name, ts);

CREATE TABLE IF NOT EXISTS alerts (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  ts       REAL NOT NULL,
  level    TEXT NOT NULL,
  source   TEXT NOT NULL,
  message  TEXT NOT NULL,
  resolved INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_alerts_source ON alerts(source, ts);
"""


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA wal_autocheckpoint=1000")
    return conn


def init_db(db_path: Path | None = None) -> sqlite3.Connection:
    """建库建表 + 预置监控节点。幂等，可重复调用；路径变化时自动重连（测试隔离用）。"""
    global _conn, _db_path
    path = Path(db_path or config.DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        if _conn is None or _db_path != path:
            if _conn is not None:
                try:
                    _conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                except sqlite3.Error:
                    pass
                _conn.close()
            _conn = _connect(path)
            _db_path = path
        _conn.executescript(SCHEMA)
        seed_nodes(config.NODES)
    return _conn


def close() -> None:
    global _conn
    with _lock:
        if _conn is not None:
            try:
                _conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:
                pass
            _conn.close()
            _conn = None


def _cursor():
    if _conn is None:
        init_db()
    return _conn


# ---------------- monitor_nodes ----------------
def seed_nodes(nodes: list[dict]) -> None:
    conn = _cursor()
    with _lock:
        for n in nodes:
            conn.execute(
                "INSERT OR IGNORE INTO monitor_nodes(id, name, role, public_ip, enabled) "
                "VALUES (?, ?, ?, ?, ?)",
                (n.get("id"), n.get("name", n.get("id")), n.get("role", "primary"),
                 n.get("public_ip"), 1 if n.get("enabled", True) else 0),
            )
        conn.commit()


def touch_node(node_id: str) -> None:
    conn = _cursor()
    with _lock:
        conn.execute("UPDATE monitor_nodes SET last_seen_ts=? WHERE id=?",
                     (time.time(), node_id))
        conn.commit()


def get_nodes() -> list[dict]:
    conn = _cursor()
    with _lock:
        rows = conn.execute("SELECT * FROM monitor_nodes ORDER BY id").fetchall()
        return [dict(r) for r in rows]


# ---------------- samples ----------------
def insert_samples(node_id: str, group: str,
                   metrics: list[tuple[str, float, float]]) -> int:
    """批量写入样本（单事务）。返回实际插入数（白名单过滤后）。"""
    conn = _cursor()
    rows = [(node_id, group, name, value, ts)
            for name, value, ts in metrics
            if constants.is_valid_metric(group, name)]
    skipped = len(metrics) - len(rows)
    if skipped:
        log.warning("samples: %d metric(s) rejected by whitelist (group=%s)", skipped, group)
    if not rows:
        return 0
    with _lock:
        conn.executemany(
            "INSERT INTO samples(node_id, metric_group, metric_name, value, ts) "
            "VALUES (?, ?, ?, ?, ?)", rows)
        conn.commit()
    return len(rows)


def query_history(metric_name: str, since_ts: float, bucket_s: float,
                  group: str | None = None) -> list[dict]:
    """按桶降采样：AVG(value)，返回桶起点时间戳。"""
    conn = _cursor()
    sql = ("SELECT CAST((ts - ?) / ? AS INTEGER) AS b, AVG(value) AS v, MAX(ts) AS ts "
           "FROM samples WHERE metric_name=? AND ts >= ? ")
    params: list = [since_ts, bucket_s, metric_name, since_ts]
    if group:
        sql += "AND metric_group=? "
        params.append(group)
    sql += "GROUP BY b ORDER BY b"
    with _lock:
        rows = conn.execute(sql, params).fetchall()
    return [{"ts": since_ts + row["b"] * bucket_s, "value": round(row["v"], 4),
             "ts_max": row["ts"]} for row in rows]


def query_latest_by_group(group: str, limit_metrics: int = 200) -> dict[str, float]:
    """每个 metric_name 取最新一条（MAX(id)）。"""
    conn = _cursor()
    sql = ("SELECT s.metric_name, s.value, s.ts FROM samples s "
           "JOIN (SELECT metric_name, MAX(id) AS mid FROM samples "
           "      WHERE metric_group=? GROUP BY metric_name) t "
           "ON s.id = t.mid ORDER BY s.metric_name LIMIT ?")
    with _lock:
        rows = conn.execute(sql, (group, limit_metrics)).fetchall()
    return {r["metric_name"]: float(r["value"]) for r in rows}


def query_sample_range(metric_name: str) -> tuple[float | None, float | None, int]:
    """指标最早/最晚 ts 与点数（趋势图轴范围用）。"""
    conn = _cursor()
    with _lock:
        row = conn.execute(
            "SELECT MIN(ts) AS lo, MAX(ts) AS hi, COUNT(*) AS n "
            "FROM samples WHERE metric_name=?", (metric_name,)).fetchone()
    return (row["lo"], row["hi"], row["n"])


# ---------------- devices / tunnels（快照表，只留最新） ----------------
_DEVICE_DEFAULTS = {
    "name": None, "tailscale_ip": None, "os": None, "online": 0, "relay": None,
    "last_seen_ts": None, "rx_bytes": 0, "tx_bytes": 0, "updated_ts": None,
}
_TUNNEL_DEFAULTS = {
    "type": None, "remote_port": None, "local_addr": None, "status": "unknown",
    "traffic_in": 0, "traffic_out": 0, "updated_ts": None,
}


def upsert_devices(node_id: str, rows: list[dict]) -> None:
    if not rows:
        return
    # 防御：统一补 node_id 与缺失字段（mock/远端上报可能缺键）
    rows = [{**_DEVICE_DEFAULTS, **r, "node_id": node_id} for r in rows]
    conn = _cursor()
    with _lock:
        conn.executemany(
            "INSERT INTO devices(node_id, device_id, name, tailscale_ip, os, online, "
            "relay, last_seen_ts, rx_bytes, tx_bytes, updated_ts) "
            "VALUES (:node_id, :device_id, :name, :tailscale_ip, :os, :online, "
            ":relay, :last_seen_ts, :rx_bytes, :tx_bytes, :updated_ts) "
            "ON CONFLICT(node_id, device_id) DO UPDATE SET "
            "name=excluded.name, tailscale_ip=excluded.tailscale_ip, os=excluded.os, "
            "online=excluded.online, relay=excluded.relay, "
            "last_seen_ts=excluded.last_seen_ts, rx_bytes=excluded.rx_bytes, "
            "tx_bytes=excluded.tx_bytes, updated_ts=excluded.updated_ts",
            rows)
        conn.commit()


def get_devices(node_id: str | None = None) -> list[dict]:
    conn = _cursor()
    sql = "SELECT * FROM devices"
    params: list = []
    if node_id:
        sql += " WHERE node_id=?"
        params.append(node_id)
    sql += " ORDER BY device_id"
    with _lock:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def upsert_tunnels(node_id: str, rows: list[dict]) -> None:
    if not rows:
        return
    rows = [{**_TUNNEL_DEFAULTS, **r, "node_id": node_id} for r in rows]
    conn = _cursor()
    with _lock:
        conn.executemany(
            "INSERT INTO tunnels(node_id, tunnel_id, type, remote_port, local_addr, "
            "status, traffic_in, traffic_out, updated_ts) "
            "VALUES (:node_id, :tunnel_id, :type, :remote_port, :local_addr, "
            ":status, :traffic_in, :traffic_out, :updated_ts) "
            "ON CONFLICT(node_id, tunnel_id) DO UPDATE SET "
            "type=excluded.type, remote_port=excluded.remote_port, "
            "local_addr=excluded.local_addr, status=excluded.status, "
            "traffic_in=excluded.traffic_in, traffic_out=excluded.traffic_out, "
            "updated_ts=excluded.updated_ts",
            rows)
        conn.commit()


def get_tunnels(node_id: str | None = None) -> list[dict]:
    conn = _cursor()
    sql = "SELECT * FROM tunnels"
    params: list = []
    if node_id:
        sql += " WHERE node_id=?"
        params.append(node_id)
    sql += " ORDER BY tunnel_id"
    with _lock:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


# ---------------- alerts ----------------
def insert_alert(level: str, source: str, message: str,
                 cooldown_s: float | None = None) -> bool:
    """带冷却插入：同一 (source, message) 在冷却窗口内不重复告警（8899 模式复用）。

    返回 True=已插入，False=冷却中跳过。
    """
    if level not in constants.ALERT_LEVELS:
        level = constants.LEVEL_WARN
    cooldown_s = cooldown_s if cooldown_s is not None else config.ALERT_COOLDOWN_S
    conn = _cursor()
    with _lock:
        row = conn.execute(
            "SELECT id FROM alerts WHERE source=? AND message=? AND resolved=0 "
            "AND ts > ? ORDER BY ts DESC LIMIT 1",
            (source, message, time.time() - cooldown_s)).fetchone()
        if row:
            return False
        conn.execute(
            "INSERT INTO alerts(ts, level, source, message, resolved) VALUES (?, ?, ?, ?, 0)",
            (time.time(), level, source, message))
        conn.commit()
    return True


def resolve_alerts(source: str) -> int:
    """条件恢复时把该 source 的未解决告警置为已解决。"""
    conn = _cursor()
    with _lock:
        cur = conn.execute("UPDATE alerts SET resolved=1 WHERE source=? AND resolved=0",
                           (source,))
        conn.commit()
    return cur.rowcount


def get_alerts(limit: int = 50, unresolved_only: bool = False) -> list[dict]:
    conn = _cursor()
    sql = "SELECT * FROM alerts"
    if unresolved_only:
        sql += " WHERE resolved=0"
    sql += " ORDER BY ts DESC LIMIT ?"
    with _lock:
        rows = conn.execute(sql, (limit,)).fetchall()
    return [dict(r) for r in rows]


def count_unresolved_alerts() -> int:
    conn = _cursor()
    with _lock:
        row = conn.execute("SELECT COUNT(*) AS n FROM alerts WHERE resolved=0").fetchone()
    return int(row["n"])


# ---------------- 清理 ----------------
def cleanup(now: float | None = None) -> dict:
    """过期样本/告警清理 + WAL checkpoint（每小时由调度器触发）。"""
    now = now or time.time()
    conn = _cursor()
    sample_cutoff = now - config.SAMPLE_RETENTION_DAYS * 86400
    alert_cutoff = now - config.ALERT_RETENTION_DAYS * 86400
    with _lock:
        cur1 = conn.execute("DELETE FROM samples WHERE ts < ?", (sample_cutoff,))
        cur2 = conn.execute("DELETE FROM alerts WHERE ts < ?", (alert_cutoff,))
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    log.info("cleanup: samples=%d alerts=%d", cur1.rowcount, cur2.rowcount)
    return {"samples": cur1.rowcount, "alerts": cur2.rowcount}
