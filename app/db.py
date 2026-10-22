"""SQLite(WAL) 存储层。

- PRAGMA：journal_mode=WAL + synchronous=NORMAL + busy_timeout=5000（评审 E12）
- 5 张表见 README §3.1 / 技术方案 §3.5；均带 node_id 支持多云
- samples 写入白名单校验（constants.is_valid_metric）+ 批量事务
- 清理：每小时 DELETE 过期 + wal_checkpoint(TRUNCATE)（低频低竞争）
"""
from __future__ import annotations

import asyncio
import json
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
  key_expiry_ts      REAL,     -- tailscale KeyExpiry（Phase 2）
  dns_name           TEXT,     -- MagicDNS 全名
  subnet_routes      TEXT,     -- JSON 数组（AllowedIPs/PrimaryRoutes 中的子网路由）
  allowed_ips        TEXT,     -- JSON 数组（原始 AllowedIPs）
  exit_node          INTEGER DEFAULT 0,  -- 当前选定的出口节点
  exit_node_option   INTEGER DEFAULT 0,  -- 可作为出口节点
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
CREATE INDEX IF NOT EXISTS idx_samples_node
  ON samples(node_id, metric_group, metric_name, ts);

CREATE TABLE IF NOT EXISTS alerts (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  ts       REAL NOT NULL,
  level    TEXT NOT NULL,
  source   TEXT NOT NULL,
  message  TEXT NOT NULL,
  resolved INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_alerts_source ON alerts(source, ts);

CREATE TABLE IF NOT EXISTS services (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  node_id       TEXT NOT NULL,
  device_id     TEXT NOT NULL,
  device_name   TEXT,
  tailscale_ip  TEXT,
  port          INT NOT NULL,
  name          TEXT,            -- 人工覆盖名（SERVICES_ALIASES）
  auto_name     TEXT,            -- 自动提取（title/server/ip:port）
  protocol      TEXT DEFAULT 'tcp',   -- tcp/http/ssh/unknown
  url           TEXT,            -- http 可达时的直达链接
  http_status   INT,
  latency_ms    REAL,
  status        TEXT DEFAULT 'unknown', -- open/closed/unknown
  missing_rounds INT DEFAULT 0,
  source        TEXT DEFAULT 'auto',    -- auto(范围扫描)/manual(固定登记)
  enabled       INT DEFAULT 1,
  first_seen_ts REAL,
  last_seen_ts  REAL,
  updated_ts    REAL,
  UNIQUE(node_id, device_id, protocol, port)
);
CREATE INDEX IF NOT EXISTS idx_services_status ON services(status);

CREATE TABLE IF NOT EXISTS grm_repos (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  node_id            TEXT NOT NULL,
  repo_id            TEXT NOT NULL,   -- grm Repository.id（<provider>:owner/repo）
  url                TEXT,
  owner_slug         TEXT,            -- owner/repo 显示名
  display_name       TEXT,            -- grm 侧用户自定义标题
  tags               TEXT,            -- JSON 数组
  is_pinned          INT DEFAULT 0,
  is_new             INT DEFAULT 0,   -- 新发布标记
  latest_tag         TEXT,
  latest_name        TEXT,
  latest_url         TEXT,
  latest_prerelease  INT DEFAULT 0,
  published_ts       REAL,
  readme_summary     TEXT,            -- grm fork 扩展字段（就绪前 NULL，前端占位）
  recent_commits     TEXT,            -- JSON 数组（就绪前 []）
  first_seen_ts      REAL,
  updated_ts         REAL,
  UNIQUE(node_id, repo_id)
);
CREATE INDEX IF NOT EXISTS idx_grm_repos_pinned ON grm_repos(is_pinned);

CREATE TABLE IF NOT EXISTS update_items (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  node_id        TEXT NOT NULL,
  provider       TEXT NOT NULL,   -- 来源（grm / 未来其他 UpdateProvider）
  item_key       TEXT NOT NULL,   -- provider 内唯一键
  title          TEXT,
  url            TEXT,
  version        TEXT,            -- 最新版本/tag
  version_url    TEXT,
  is_new         INT DEFAULT 0,   -- 新发布标记
  prerelease     INT DEFAULT 0,
  tags           TEXT,            -- JSON 数组
  published_ts   REAL,
  updated_ts     REAL,
  UNIQUE(node_id, provider, item_key)
);
CREATE INDEX IF NOT EXISTS idx_update_items ON update_items(provider, is_new);

CREATE TABLE IF NOT EXISTS taishan_snapshot (
  node_id    TEXT PRIMARY KEY,   -- 上报节点（dc-pi）
  payload    TEXT NOT NULL,      -- JSON：active_node/state/summary/top_candidates/...
  updated_ts REAL NOT NULL
);
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
        _migrate(_conn)
        seed_nodes(config.NODES)
    return _conn


_DEVICE_MIGRATION_COLS = {
    "key_expiry_ts": "REAL", "dns_name": "TEXT", "subnet_routes": "TEXT",
    "allowed_ips": "TEXT", "exit_node": "INTEGER DEFAULT 0",
    "exit_node_option": "INTEGER DEFAULT 0",
}


def _migrate_services_unique_protocol(conn: sqlite3.Connection) -> None:
    """services 唯一键 (node_id, device_id, port) → 加 protocol。

    TCP/UDP 同号端口（如 53）在旧键下会互相覆盖；SQLite 无法改 UNIQUE 约束，
    只能重建表。幂等：已是新键则直接返回。
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='services'"
    ).fetchone()
    if not row or "UNIQUE(node_id, device_id, protocol, port)" in (row["sql"] or ""):
        return
    log.info("db: migrate services unique key += protocol（重建表）")
    conn.executescript("""
        BEGIN;
        CREATE TABLE services_new (
          id            INTEGER PRIMARY KEY AUTOINCREMENT,
          node_id       TEXT NOT NULL,
          device_id     TEXT NOT NULL,
          device_name   TEXT,
          tailscale_ip  TEXT,
          port          INT NOT NULL,
          name          TEXT,
          auto_name     TEXT,
          protocol      TEXT DEFAULT 'tcp',
          url           TEXT,
          http_status   INT,
          latency_ms    REAL,
          status        TEXT DEFAULT 'unknown',
          missing_rounds INT DEFAULT 0,
          source        TEXT DEFAULT 'auto',
          enabled       INT DEFAULT 1,
          first_seen_ts REAL,
          last_seen_ts  REAL,
          updated_ts    REAL,
          UNIQUE(node_id, device_id, protocol, port)
        );
        INSERT INTO services_new
          SELECT id, node_id, device_id, device_name, tailscale_ip, port, name,
                 auto_name, COALESCE(protocol, 'tcp'), url, http_status, latency_ms,
                 status, missing_rounds, source, enabled, first_seen_ts,
                 last_seen_ts, updated_ts FROM services;
        DROP TABLE services;
        ALTER TABLE services_new RENAME TO services;
        CREATE INDEX IF NOT EXISTS idx_services_status ON services(status);
        COMMIT;
    """)
    log.info("db: services 表重建完成")


def _migrate(conn: sqlite3.Connection) -> None:
    """轻量列迁移：SCHEMA 用 CREATE IF NOT EXISTS，旧库新列需 ALTER 补齐。"""
    try:
        _migrate_services_unique_protocol(conn)
    except sqlite3.Error:
        log.error("db: migrate services unique key failed", exc_info=True)
    for col, decl in _DEVICE_MIGRATION_COLS.items():
        try:
            cols = {r["name"] for r in conn.execute(
                "PRAGMA table_info(devices)").fetchall()}
            if col not in cols:
                conn.execute(f"ALTER TABLE devices ADD COLUMN {col} {decl}")
        except sqlite3.Error:
            log.warning("db: migrate devices.%s failed", col, exc_info=True)


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


def auto_register_node(node_id: str) -> None:
    """tailnet 成员首次上报时自动登记 monitor_nodes（"入 tailnet 即注册"）。

    设备表已有同名主机（tailscale 采集），此处补 monitor_nodes 行使
    foot-node/节点切换可见；不覆盖已有行。
    """
    conn = _cursor()
    with _lock:
        conn.execute(
            "INSERT OR IGNORE INTO monitor_nodes(id, name, role, public_ip, enabled) "
            "VALUES (?, ?, 'secondary', NULL, 1)", (node_id, node_id))
        conn.commit()


def touch_node(node_id: str) -> None:
    conn = _cursor()
    with _lock:
        conn.execute("UPDATE monitor_nodes SET last_seen_ts=? WHERE id=?",
                     (time.time(), node_id))
        conn.commit()


def node_exists(node_id: str) -> bool:
    conn = _cursor()
    with _lock:
        row = conn.execute("SELECT 1 FROM monitor_nodes WHERE id=?", (node_id,)).fetchone()
        return row is not None


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
                  group: str | None = None,
                  node_id: str | None = None) -> list[dict]:
    """按桶降采样：AVG(value)，返回桶起点时间戳。"""
    conn = _cursor()
    sql = ("SELECT CAST((ts - ?) / ? AS INTEGER) AS b, AVG(value) AS v, MAX(ts) AS ts "
           "FROM samples WHERE metric_name=? AND ts >= ? ")
    params: list = [since_ts, bucket_s, metric_name, since_ts]
    if node_id is not None:
        sql += "AND node_id=? "
        params.append(node_id)
    if group:
        sql += "AND metric_group=? "
        params.append(group)
    sql += "GROUP BY b ORDER BY b"
    with _lock:
        rows = conn.execute(sql, params).fetchall()
    return [{"ts": since_ts + row["b"] * bucket_s, "value": round(row["v"], 4),
             "ts_max": row["ts"]} for row in rows]


def query_latest_by_group(group: str, limit_metrics: int = 200,
                          node_id: str | None = None) -> dict[str, float]:
    """每个 metric_name 取最新一条（MAX(id)）。

    非热路径：全组 MAX(id) 扫描；生产读取用 Aggregator.latest_for / query_latest_metrics。
    """
    conn = _cursor()
    if node_id is not None:
        sql = ("SELECT s.metric_name, s.value, s.ts FROM samples s "
               "JOIN (SELECT metric_name, MAX(id) AS mid FROM samples "
               "      WHERE metric_group=? AND node_id=? GROUP BY metric_name) t "
               "ON s.id = t.mid WHERE s.node_id=? ORDER BY s.metric_name LIMIT ?")
        params = (group, node_id, node_id, limit_metrics)
    else:
        sql = ("SELECT s.metric_name, s.value, s.ts FROM samples s "
               "JOIN (SELECT metric_name, MAX(id) AS mid FROM samples "
               "      WHERE metric_group=? GROUP BY metric_name) t "
               "ON s.id = t.mid ORDER BY s.metric_name LIMIT ?")
        params = (group, limit_metrics)
    with _lock:
        rows = conn.execute(sql, params).fetchall()
    return {r["metric_name"]: float(r["value"]) for r in rows}


def query_latest_metrics(group: str, names: list[str],
                         node_id: str | None = None) -> dict[str, float]:
    """按指标名逐条取最新一行（索引点查，热路径安全）。

    走 idx_samples_node / idx_samples_lookup 的等值前缀 + ORDER BY ts DESC LIMIT 1，
    避免 query_latest_by_group 的全组 MAX(id) 扫描；names 循环单条查询。
    """
    conn = _cursor()
    if node_id is not None:
        sql = ("SELECT value FROM samples WHERE node_id=? AND metric_group=? "
               "AND metric_name=? ORDER BY ts DESC LIMIT 1")
    else:
        sql = ("SELECT value FROM samples WHERE metric_group=? AND metric_name=? "
               "ORDER BY ts DESC LIMIT 1")
    out: dict[str, float] = {}
    with _lock:
        for name in names:
            params = (node_id, group, name) if node_id is not None else (group, name)
            row = conn.execute(sql, params).fetchone()
            if row is not None:
                out[name] = float(row["value"])
    return out


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
    "key_expiry_ts": None, "dns_name": None, "subnet_routes": None,
    "allowed_ips": None, "exit_node": 0, "exit_node_option": 0,
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
    for r in rows:
        # JSON 数组字段序列化（allowed_ips/subnet_routes 传 list 或 JSON 字符串均可）
        for col in ("allowed_ips", "subnet_routes"):
            v = r.get(col)
            if isinstance(v, list):
                r[col] = json.dumps(v, ensure_ascii=False)
            elif v is None:
                r[col] = "[]"
        r["exit_node"] = 1 if r.get("exit_node") else 0
        r["exit_node_option"] = 1 if r.get("exit_node_option") else 0
    conn = _cursor()
    with _lock:
        conn.executemany(
            "INSERT INTO devices(node_id, device_id, name, tailscale_ip, os, online, "
            "relay, last_seen_ts, rx_bytes, tx_bytes, updated_ts, "
            "key_expiry_ts, dns_name, subnet_routes, allowed_ips, "
            "exit_node, exit_node_option) "
            "VALUES (:node_id, :device_id, :name, :tailscale_ip, :os, :online, "
            ":relay, :last_seen_ts, :rx_bytes, :tx_bytes, :updated_ts, "
            ":key_expiry_ts, :dns_name, :subnet_routes, :allowed_ips, "
            ":exit_node, :exit_node_option) "
            "ON CONFLICT(node_id, device_id) DO UPDATE SET "
            "name=excluded.name, tailscale_ip=excluded.tailscale_ip, os=excluded.os, "
            "online=excluded.online, relay=excluded.relay, "
            "last_seen_ts=excluded.last_seen_ts, rx_bytes=excluded.rx_bytes, "
            "tx_bytes=excluded.tx_bytes, updated_ts=excluded.updated_ts, "
            "key_expiry_ts=excluded.key_expiry_ts, dns_name=excluded.dns_name, "
            "subnet_routes=excluded.subnet_routes, "
            "allowed_ips=excluded.allowed_ips, "
            "exit_node=excluded.exit_node, "
            "exit_node_option=excluded.exit_node_option",
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


# ---------------- taishan_snapshot（TaiShan 出网质量：单行/节点，跨重启保留） ----------------
def upsert_taishan_snapshot(node_id: str, payload: dict, ts: float) -> None:
    """taishan payload 快照（单行/节点）。ON CONFLICT(node_id) DO UPDATE。"""
    conn = _cursor()
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    with _lock:
        conn.execute(
            "INSERT INTO taishan_snapshot(node_id, payload, updated_ts) VALUES (?, ?, ?) "
            "ON CONFLICT(node_id) DO UPDATE SET payload=excluded.payload, "
            "updated_ts=excluded.updated_ts",
            (node_id, body, ts))
        conn.commit()


def get_taishan_snapshot(node_id: str) -> dict | None:
    """PK 点查：返回 payload dict；payload 无 ts 时回填快照写入时刻。"""
    conn = _cursor()
    with _lock:
        row = conn.execute(
            "SELECT payload, updated_ts FROM taishan_snapshot WHERE node_id=?",
            (node_id,)).fetchone()
    if row is None:
        return None
    data = json.loads(row["payload"])
    if not isinstance(data, dict):
        return None
    data.setdefault("ts", row["updated_ts"])
    return data


def has_taishan_snapshot() -> bool:
    """是否曾有过上报（停报看门狗区分冷启动与真停报）。"""
    conn = _cursor()
    with _lock:
        row = conn.execute("SELECT 1 FROM taishan_snapshot LIMIT 1").fetchone()
    return row is not None


def taishan_updated_ts() -> float | None:
    """快照表最近写入时刻（重启后内存无心跳时的看门狗基准）。"""
    conn = _cursor()
    with _lock:
        row = conn.execute(
            "SELECT MAX(updated_ts) AS ts FROM taishan_snapshot").fetchone()
    return float(row["ts"]) if row and row["ts"] is not None else None


# ---------------- services（服务台账：自动发现 + 探活快照） ----------------
_SERVICE_DEFAULTS = {
    "device_name": None, "tailscale_ip": None, "name": None, "auto_name": None,
    "protocol": "tcp", "url": None, "http_status": None, "latency_ms": None,
    "status": "unknown", "missing_rounds": 0, "source": "auto", "enabled": 1,
    "first_seen_ts": None, "last_seen_ts": None, "updated_ts": None,
}


def upsert_services(node_id: str, rows: list[dict]) -> None:
    """服务台账 upsert：UNIQUE(node_id, device_id, port)。

    已存在行保留 first_seen_ts；人工覆盖名（name）不被自动名覆盖；
    自动发现（auto）与固定登记（manual）统一入口。
    """
    if not rows:
        return
    rows = [{**_SERVICE_DEFAULTS, **r, "node_id": node_id} for r in rows]
    conn = _cursor()
    with _lock:
        conn.executemany(
            "INSERT INTO services(node_id, device_id, device_name, tailscale_ip, port, "
            "name, auto_name, protocol, url, http_status, latency_ms, status, "
            "missing_rounds, source, enabled, first_seen_ts, last_seen_ts, updated_ts) "
            "VALUES (:node_id, :device_id, :device_name, :tailscale_ip, :port, "
            ":name, :auto_name, :protocol, :url, :http_status, :latency_ms, :status, "
            ":missing_rounds, :source, :enabled, :first_seen_ts, :last_seen_ts, :updated_ts) "
            "ON CONFLICT(node_id, device_id, protocol, port) DO UPDATE SET "
            "device_name=excluded.device_name, tailscale_ip=excluded.tailscale_ip, "
            "name=COALESCE(excluded.name, services.name), "
            "auto_name=excluded.auto_name, protocol=excluded.protocol, url=excluded.url, "
            "http_status=excluded.http_status, latency_ms=excluded.latency_ms, "
            "status=excluded.status, missing_rounds=excluded.missing_rounds, "
            "source=CASE WHEN services.source IN ('manual','agent') "
            "THEN services.source ELSE excluded.source END, "
            "enabled=CASE WHEN services.source IN ('manual','agent') "
            "THEN services.enabled ELSE excluded.enabled END, "
            "last_seen_ts=excluded.last_seen_ts, updated_ts=excluded.updated_ts",
            rows)
        conn.commit()


def get_services(node_id: str | None = None,
                 status: str | None = None) -> list[dict]:
    conn = _cursor()
    sql = "SELECT * FROM services"
    params: list = []
    conds = []
    if node_id:
        conds.append("node_id=?")
        params.append(node_id)
    if status:
        conds.append("status=?")
        params.append(status)
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    sql += " ORDER BY tailscale_ip, port"
    with _lock:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def delete_services(ids: list[int]) -> int:
    """按主键删服务行（prune 用；调用方负责同步 resolve 告警）。"""
    if not ids:
        return 0
    conn = _cursor()
    with _lock:
        marks = ",".join("?" * len(ids))
        cur = conn.execute(f"DELETE FROM services WHERE id IN ({marks})", ids)
        conn.commit()
    return cur.rowcount


def delete_agent_services_except(node_id: str,
                                 keep_ports: set) -> list[dict]:
    """agent 上报的替换语义：删除该节点下未出现在本轮清单里的 agent 行。

    keep_ports 元素可为 (proto, port) 元组或 int（向后兼容）。
    只认 source='agent'——manual 行由人工维护，agent 无权删除。
    返回被删行（id/tailscale_ip/port/protocol），供调用方 resolve 对应告警。
    """
    conn = _cursor()
    with _lock:
        rows = conn.execute(
            "SELECT id, tailscale_ip, port, protocol FROM services "
            "WHERE node_id=? AND source='agent'", (node_id,)).fetchall()

        def _kept(r) -> bool:
            port = r["port"]
            proto = r["protocol"] if "protocol" in r.keys() else "tcp"
            return (proto or "tcp", port) in keep_ports or port in keep_ports

        doomed = [dict(r) for r in rows if not _kept(r)]
        if doomed:
            marks = ",".join("?" * len(doomed))
            conn.execute(f"DELETE FROM services WHERE id IN ({marks})",
                         [r["id"] for r in doomed])
            conn.commit()
    return doomed


# ---------------- grm（Release 监控快照：issue #4） ----------------
_GRM_DEFAULTS = {
    "url": None, "owner_slug": None, "display_name": None, "tags": "[]",
    "is_pinned": 0, "is_new": 0, "latest_tag": None, "latest_name": None,
    "latest_url": None, "latest_prerelease": 0, "published_ts": None,
    "readme_summary": None, "recent_commits": "[]",
    "first_seen_ts": None, "updated_ts": None,
}


def upsert_grm_repos(node_id: str, rows: list[dict],
                     prune: bool = True) -> None:
    """Release 监控快照 upsert：UNIQUE(node_id, repo_id)，保留 first_seen_ts。

    prune=True 时清除本轮未出现的行——grm 是仓库清单的唯一权威源，
    仓库在 grm 侧删除后本表同步消失（degraded 轮次由调用方跳过 prune）。
    """
    rows = [{**_GRM_DEFAULTS, **r, "node_id": node_id} for r in rows]
    conn = _cursor()
    with _lock:
        if rows:
            conn.executemany(
                "INSERT INTO grm_repos(node_id, repo_id, url, owner_slug, "
                "display_name, tags, is_pinned, is_new, latest_tag, latest_name, "
                "latest_url, latest_prerelease, published_ts, readme_summary, "
                "recent_commits, first_seen_ts, updated_ts) "
                "VALUES (:node_id, :repo_id, :url, :owner_slug, :display_name, "
                ":tags, :is_pinned, :is_new, :latest_tag, :latest_name, "
                ":latest_url, :latest_prerelease, :published_ts, :readme_summary, "
                ":recent_commits, :first_seen_ts, :updated_ts) "
                "ON CONFLICT(node_id, repo_id) DO UPDATE SET "
                "url=excluded.url, owner_slug=excluded.owner_slug, "
                "display_name=excluded.display_name, tags=excluded.tags, "
                "is_pinned=excluded.is_pinned, is_new=excluded.is_new, "
                "latest_tag=excluded.latest_tag, latest_name=excluded.latest_name, "
                "latest_url=excluded.latest_url, "
                "latest_prerelease=excluded.latest_prerelease, "
                "published_ts=excluded.published_ts, "
                "readme_summary=excluded.readme_summary, "
                "recent_commits=excluded.recent_commits, updated_ts=excluded.updated_ts",
                rows)
        if prune:
            seen = [r["repo_id"] for r in rows]
            if seen:
                marks = ",".join("?" * len(seen))
                conn.execute(
                    f"DELETE FROM grm_repos WHERE node_id=? "
                    f"AND repo_id NOT IN ({marks})", [node_id, *seen])
            else:
                conn.execute("DELETE FROM grm_repos WHERE node_id=?", [node_id])
        conn.commit()


def get_grm_repos(node_id: str | None = None) -> list[dict]:
    conn = _cursor()
    sql = ("SELECT * FROM grm_repos"
           + (" WHERE node_id=?" if node_id else ""))
    sql += " ORDER BY is_pinned DESC, owner_slug"
    with _lock:
        rows = conn.execute(sql, [node_id] if node_id else []).fetchall()
    return [dict(r) for r in rows]


# ---------------- update_items（Phase 3：服务更新多源统一快照） ----------------
_UPDATE_ITEM_DEFAULTS = {
    "title": None, "url": None, "version": None, "version_url": None,
    "is_new": 0, "prerelease": 0, "tags": "[]", "published_ts": None,
    "updated_ts": None,
}


def upsert_update_items(node_id: str, rows: list[dict],
                        prune: bool = False) -> None:
    """UpdateItem 统一入库：UNIQUE(node_id, provider, item_key)。

    prune=True 时删除该 node 下本轮未出现的行（provider 自治清单语义）。
    """
    if not rows:
        if prune:
            conn = _cursor()
            with _lock:
                conn.execute("DELETE FROM update_items WHERE node_id=?", (node_id,))
                conn.commit()
        return
    rows = [{**_UPDATE_ITEM_DEFAULTS, **r, "node_id": node_id} for r in rows]
    conn = _cursor()
    with _lock:
        conn.executemany(
            "INSERT INTO update_items(node_id, provider, item_key, title, url, "
            "version, version_url, is_new, prerelease, tags, published_ts, updated_ts) "
            "VALUES (:node_id, :provider, :item_key, :title, :url, :version, "
            ":version_url, :is_new, :prerelease, :tags, :published_ts, :updated_ts) "
            "ON CONFLICT(node_id, provider, item_key) DO UPDATE SET "
            "title=excluded.title, url=excluded.url, version=excluded.version, "
            "version_url=excluded.version_url, is_new=excluded.is_new, "
            "prerelease=excluded.prerelease, tags=excluded.tags, "
            "published_ts=excluded.published_ts, updated_ts=excluded.updated_ts",
            rows)
        if prune:
            seen = {(r["provider"], r["item_key"]) for r in rows}
            keep_ids = [row["id"] for row in conn.execute(
                "SELECT id, provider, item_key FROM update_items "
                "WHERE node_id=?", (node_id,)).fetchall()
                if (row["provider"], row["item_key"]) in seen]
            if keep_ids:
                marks = ",".join("?" * len(keep_ids))
                conn.execute(
                    f"DELETE FROM update_items WHERE node_id=? "
                    f"AND id NOT IN ({marks})", [node_id, *keep_ids])
            else:
                conn.execute("DELETE FROM update_items WHERE node_id=?", (node_id,))
        conn.commit()


def get_update_items(node_id: str | None = None,
                     provider: str | None = None) -> list[dict]:
    conn = _cursor()
    sql = "SELECT * FROM update_items"
    conds, params = [], []
    if node_id:
        conds.append("node_id=?")
        params.append(node_id)
    if provider:
        conds.append("provider=?")
        params.append(provider)
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    sql += " ORDER BY is_new DESC, published_ts DESC"
    with _lock:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


# ---------------- alerts ----------------
def insert_alert(level: str, source: str, message: str,
                 cooldown_s: float | None = None) -> bool:
    """带冷却插入 + 未解决去重（2026-08-24 审查修复）。

    语义（两层）：
    1. 该 source 已有**未解决**告警 → 只刷新其 ts（持续故障保持单条 active 行，
       不再随冷却到期线性堆积——曾致 7 天 310 条同源未解决告警淹没真实告警）
    2. 无未解决行但冷却窗口内有同 (source, message) 的已解决记录 → 冷却跳过
       （恢复→再故障场景仍受冷却抑制）

    返回 True=新插入，False=去重/冷却跳过。
    """
    if level not in constants.ALERT_LEVELS:
        level = constants.LEVEL_WARN
    cooldown_s = cooldown_s if cooldown_s is not None else config.ALERT_COOLDOWN_S
    conn = _cursor()
    with _lock:
        now = time.time()
        active = conn.execute(
            "SELECT id FROM alerts WHERE source=? AND resolved=0 "
            "ORDER BY ts DESC LIMIT 1", (source,)).fetchone()
        if active:
            conn.execute("UPDATE alerts SET ts=?, message=? WHERE id=?",
                         (now, message, active["id"]))
            conn.commit()
            return False
        row = conn.execute(
            "SELECT id FROM alerts WHERE source=? AND message=? "
            "AND ts > ? ORDER BY ts DESC LIMIT 1",
            (source, message, now - cooldown_s)).fetchone()
        if row:
            return False
        conn.execute(
            "INSERT INTO alerts(ts, level, source, message, resolved) VALUES (?, ?, ?, ?, 0)",
            (now, level, source, message))
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


def dedup_unresolved_alerts() -> int:
    """同 source 未解决告警压缩为最新一条（2026-08-24 审查：历史堆积治理）。

    insert_alert 去重只防新增不清理存量（曾 7 天堆出同源 310 条）；
    服务启动时调用一次，幂等。
    """
    conn = _cursor()
    with _lock:
        cur = conn.execute(
            "UPDATE alerts SET resolved=1 WHERE resolved=0 AND id NOT IN "
            "(SELECT MAX(id) FROM alerts WHERE resolved=0 GROUP BY source)")
        conn.commit()
    return cur.rowcount


# ---------------- 清理 ----------------
def cleanup(now: float | None = None) -> dict:
    """过期样本/告警清理 + 设备残留清理 + WAL checkpoint（每小时由调度器触发）。"""
    now = now or time.time()
    conn = _cursor()
    sample_cutoff = now - config.SAMPLE_RETENTION_DAYS * 86400
    alert_cutoff = now - config.ALERT_RETENTION_DAYS * 86400
    # 设备残留：连续 7 天未出现在 tailscale status 的行（重注册/退网后旧行
    # 会永久残留并污染设备表——2026-10-20 面板实录 dc-laptop 双行）
    device_cutoff = now - config.DEVICE_RESIDENCE_DAYS * 86400
    with _lock:
        cur1 = conn.execute("DELETE FROM samples WHERE ts < ?", (sample_cutoff,))
        cur2 = conn.execute("DELETE FROM alerts WHERE ts < ?", (alert_cutoff,))
        cur3 = conn.execute("DELETE FROM devices WHERE updated_ts < ?", (device_cutoff,))
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    log.info("cleanup: samples=%d alerts=%d devices=%d",
             cur1.rowcount, cur2.rowcount, cur3.rowcount)
    return {"samples": cur1.rowcount, "alerts": cur2.rowcount,
            "devices": cur3.rowcount}


async def cleanup_async(now: float | None = None) -> dict:
    """cleanup 的异步封装：DELETE + checkpoint 是阻塞调用，走 to_thread
    防止调度器事件循环被磁盘 IO 卡住（Phase 4）。"""
    return await asyncio.to_thread(cleanup, now)
