"""多节点基础架构测试（Pkg 2）：
1. 远端不覆盖本地快照，支持 ?node= 查询对应节点
2. latest_for 单元测试
3. 未知节点整批 400（原子性：混合未知节点整批不写）
4. 已注册节点 touch_node 心跳通电（last_seen_ts 更新）
5. /api/history 多节点过滤：缺省本机、?node=指定、?node=all 聚合
6. /api/system 快照空时无条件回退 samples 最新值（#14）
"""
import time
import pytest
from app import config, constants, db
from app.api import set_aggregator
from app.collectors.base import CollectorResult
from app.services.aggregator import Aggregator


def test_remote_does_not_overwrite_local(client):
    """远端不覆盖本地快照：写入 cloud-1 与 cloud-2，缺省读 cloud-1，?node=cloud-2 读远端。"""
    agg = Aggregator()
    set_aggregator(agg)

    r1 = CollectorResult(node_id="cloud-1", group=constants.GROUP_SYSTEM)
    r1.payload = {"cpu_percent": 15.0, "mem_percent": 40.0}
    r2 = CollectorResult(node_id="cloud-2", group=constants.GROUP_SYSTEM)
    r2.payload = {"cpu_percent": 85.0, "mem_percent": 90.0}

    agg._ingest_blocking(r1, time.time())
    agg._ingest_blocking(r2, time.time())

    # 缺省返回本机（cloud-1）
    resp1 = client.get("/api/system")
    assert resp1.status_code == 200
    assert resp1.json()["data"]["cpu_percent"] == 15.0

    # 显式 ?node=cloud-2 返回远端
    resp2 = client.get("/api/system", params={"node": "cloud-2"})
    assert resp2.status_code == 200
    assert resp2.json()["data"]["cpu_percent"] == 85.0


def test_latest_for_direct():
    """latest_for 按 (node_id, group) 分节点取数。"""
    agg = Aggregator()
    r_local = CollectorResult(node_id="cloud-1", group=constants.GROUP_NETWORK)
    r_local.payload = {"probes": [{"name": "local-probe"}]}
    r_remote = CollectorResult(node_id="cloud-2", group=constants.GROUP_NETWORK)
    r_remote.payload = {"probes": [{"name": "remote-probe"}]}

    agg._ingest_blocking(r_local, time.time())
    agg._ingest_blocking(r_remote, time.time())

    assert agg.latest_for(constants.GROUP_NETWORK)["probes"][0]["name"] == "local-probe"
    assert agg.latest_for(constants.GROUP_NETWORK, "cloud-1")["probes"][0]["name"] == "local-probe"
    assert agg.latest_for(constants.GROUP_NETWORK, "cloud-2")["probes"][0]["name"] == "remote-probe"
    assert agg.latest_for(constants.GROUP_NETWORK, "unknown-node") == {}


def test_ingest_unknown_node_atomicity_and_400(client, monkeypatch):
    """未知节点拒绝并返回 400（含 rejected/registered）；混合未知节点时整批不写。"""
    monkeypatch.setattr(config, "INGEST_ENABLED", True)
    monkeypatch.setattr(config, "INGEST_TOKEN", "test-token")

    # 确认初始状态只有 cloud-1，清空已知 samples 获得基线
    conn = db._cursor()
    with db._lock:
        conn.execute("DELETE FROM samples WHERE metric_group='system'")
        conn.commit()

    nodes = [n["id"] for n in db.get_nodes()]
    assert "cloud-1" in nodes
    assert "unknown-node" not in nodes

    headers = {"Authorization": "Bearer test-token"}
    ts = time.time()

    body = {
        "results": [
            {
                "node_id": "cloud-1",
                "group": "system",
                "metrics": [{"name": "cpu_percent", "value": 11.0, "ts": ts}],
            },
            {
                "node_id": "unknown-node",
                "group": "system",
                "metrics": [{"name": "cpu_percent", "value": 99.0, "ts": ts}],
            },
        ]
    }

    r = client.post("/api/ingest", json=body, headers=headers)
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert "unknown node_id" in detail["error"]
    assert "unknown-node" in detail["rejected"]
    assert "cloud-1" in detail["registered"]

    # 原子性：混合批被拒绝，cloud-1 的 sample 也不应写入
    _, _, count = db.query_sample_range("cpu_percent")
    assert count == 0


def test_ingest_accepted_touches_node(client, monkeypatch):
    """已注册节点写入时更新 monitor_nodes 的 last_seen_ts。"""
    monkeypatch.setattr(config, "INGEST_ENABLED", True)
    monkeypatch.setattr(config, "INGEST_TOKEN", "test-token")

    conn = db._cursor()
    with db._lock:
        conn.execute("DELETE FROM monitor_nodes WHERE id='cloud-2'")
        conn.commit()

    db.seed_nodes([{"id": "cloud-2", "name": "cloud-2", "role": "secondary"}])
    node_before = next(n for n in db.get_nodes() if n["id"] == "cloud-2")
    assert node_before["last_seen_ts"] is None

    headers = {"Authorization": "Bearer test-token"}
    body = {
        "results": [
            {
                "node_id": "cloud-2",
                "group": "system",
                "metrics": [{"name": "cpu_percent", "value": 25.0, "ts": time.time()}],
            }
        ]
    }

    r = client.post("/api/ingest", json=body, headers=headers)
    assert r.status_code == 200
    assert r.json()["data"]["accepted"] == 1

    node_after = next(n for n in db.get_nodes() if n["id"] == "cloud-2")
    assert node_after["last_seen_ts"] is not None
    assert node_after["last_seen_ts"] > 0


def test_api_history_node_filtering(client):
    """/api/history 支持缺省本机、?node=指定节点、?node=all 聚合。"""
    # 清空在 lifespan 启动时由 mock 采集器插入的初始 system 样本，确保确定性
    conn = db._cursor()
    with db._lock:
        conn.execute("DELETE FROM samples WHERE metric_group='system'")
        conn.commit()

    ts = time.time()
    db.insert_samples("cloud-1", "system", [("cpu_percent", 20.0, ts)])
    db.insert_samples("cloud-2", "system", [("cpu_percent", 60.0, ts)])

    # 1. 缺省：只有本机 cloud-1 (20.0)
    r_default = client.get("/api/history", params={"metric": "cpu_percent", "group": "system", "range": "1h"})
    assert r_default.status_code == 200
    pts_default = r_default.json()["data"]["points"]
    assert len(pts_default) == 1
    assert pts_default[0]["value"] == 20.0

    # 2. ?node=cloud-2：只有远端 (60.0)
    r_remote = client.get("/api/history", params={"metric": "cpu_percent", "group": "system", "range": "1h", "node": "cloud-2"})
    assert r_remote.status_code == 200
    pts_remote = r_remote.json()["data"]["points"]
    assert len(pts_remote) == 1
    assert pts_remote[0]["value"] == 60.0

    # 3. ?node=all：显式跨节点聚合 (AVG(20, 60) = 40.0)
    r_all = client.get("/api/history", params={"metric": "cpu_percent", "group": "system", "range": "1h", "node": "all"})
    assert r_all.status_code == 200
    pts_all = r_all.json()["data"]["points"]
    assert len(pts_all) == 1
    assert pts_all[0]["value"] == 40.0


def test_api_system_restart_fallback(client):
    """#14：快照为空时无条件回退 samples 最新值。"""
    agg = Aggregator()
    set_aggregator(agg)

    ts = time.time()
    db.insert_samples("cloud-1", "system", [("cpu_percent", 33.3, ts), ("mem_percent", 55.5, ts)])

    r = client.get("/api/system")
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["cpu_percent"] == 33.3
    assert data["mem_percent"] == 55.5


def test_frp_from_remote_node(client, monkeypatch):
    """seed/注册 aliyun 节点 → 经 agg.ingest 一个 node_id=aliyun、group=frp_traffic、payload 含 tunnels 的 result
    → 断言 /api/frp?node=aliyun 返回其 tunnels、/api/frp 缺省仍本机、db.tunnels 该 node 行存在。
    """
    agg = Aggregator()
    set_aggregator(agg)

    # 1. seed / 注册 aliyun 节点
    db.seed_nodes([{"id": "aliyun", "name": "aliyun", "role": "secondary"}])
    nodes = [n["id"] for n in db.get_nodes()]
    assert "aliyun" in nodes

    ts = time.time()
    aliyun_tunnels = [
        {
            "tunnel_id": "aliyun-web",
            "type": "tcp",
            "remote_port": 18080,
            "local_addr": "127.0.0.1:8080",
            "status": "online",
            "traffic_in": 1000,
            "traffic_out": 2000,
        }
    ]

    # 本机 cloud-1 也灌入一条区分
    r_local = CollectorResult(node_id="cloud-1", group=constants.GROUP_FRP_TRAFFIC)
    r_local.payload = {
        "source": "frps-dashboard",
        "ts": ts,
        "tunnels": [
            {
                "tunnel_id": "local-ssh",
                "type": "tcp",
                "remote_port": 2222,
                "local_addr": "127.0.0.1:22",
                "status": "online",
                "traffic_in": 300,
                "traffic_out": 400,
            }
        ],
    }

    r_aliyun = CollectorResult(node_id="aliyun", group=constants.GROUP_FRP_TRAFFIC)
    r_aliyun.payload = {
        "source": "frps-dashboard",
        "ts": ts,
        "tunnels": aliyun_tunnels,
    }

    agg._ingest_blocking(r_local, ts)
    agg._ingest_blocking(r_aliyun, ts)

    # 2. 断言 /api/frp 缺省仍本机 (cloud-1)
    r_def = client.get("/api/frp")
    assert r_def.status_code == 200
    tunnels_def = r_def.json()["data"]["tunnels"]
    assert len(tunnels_def) == 1
    assert tunnels_def[0]["tunnel_id"] == "local-ssh"

    # 3. 断言 /api/frp?node=aliyun 返回其 tunnels
    r_remote = client.get("/api/frp", params={"node": "aliyun"})
    assert r_remote.status_code == 200
    tunnels_remote = r_remote.json()["data"]["tunnels"]
    assert len(tunnels_remote) == 1
    assert tunnels_remote[0]["tunnel_id"] == "aliyun-web"
    assert tunnels_remote[0]["remote_port"] == 18080
    assert tunnels_remote[0]["status"] == "online"

    # 4. 断言 db.tunnels 该 node 行存在
    db_rows = db.get_tunnels("aliyun")
    assert len(db_rows) == 1
    assert db_rows[0]["tunnel_id"] == "aliyun-web"
    assert db_rows[0]["node_id"] == "aliyun"



def test_frp_fallback_stale_rows(client):
    """隧道 DB 回退行超 15 分钟 → 标 stale（不再伪装 online）。"""
    from app import db as app_db
    old = time.time() - 20 * 86400
    app_db.upsert_tunnels("cloud-1", [{
        "tunnel_id": "legacy", "type": "tcp", "remote_port": 20099,
        "status": "online", "traffic_in": 1.0, "traffic_out": 2.0,
    }])
    conn = app_db._cursor()
    with app_db._lock:
        conn.execute("UPDATE tunnels SET updated_ts=? WHERE tunnel_id='legacy'", (old,))
        conn.commit()
    body = client.get("/api/frp").json()["data"]
    row = next(t for t in body["tunnels"] if t["tunnel_id"] == "legacy")
    assert row["status"] == "stale"


def test_tailnet_member_auto_registers(client, monkeypatch):
    """入 tailnet 即注册：devices 表成员（tailscale 主机名）首次上报自动登记
    monitor_nodes 并接受；完全未知的节点 → 整批 400。"""
    agg = Aggregator()
    set_aggregator(agg)
    monkeypatch.setattr(config, "INGEST_ENABLED", True)
    monkeypatch.setattr(config, "INGEST_TOKEN", "test-token")
    conn = db._cursor()
    with db._lock:
        conn.execute("DELETE FROM monitor_nodes WHERE id='aliyun'")
        conn.commit()
    db.upsert_devices("cloud-1", [{"device_id": "ts-aliyun", "name": "aliyun",
                                   "os": "linux", "online": True}])
    assert not db.node_exists("aliyun")

    body = {"results": [{"node_id": "aliyun", "group": "frp_traffic",
                         "metrics": [{"name": "cur_conns", "value": 3}],
                         "payload": {"tunnels": [{"tunnel_id": "web", "type": "tcp",
                                                  "remote_port": 8443, "status": "online"}]}}]}
    r = client.post("/api/ingest", json=body,
                   headers={"Authorization": "Bearer test-token"})
    assert r.status_code == 200, r.text
    assert db.node_exists("aliyun")                      # 自动登记
    row = [n for n in db.get_nodes() if n["id"] == "aliyun"][0]
    assert row["last_seen_ts"] is not None               # 心跳
    frp = client.get("/api/frp?node=aliyun").json()["data"]
    assert frp["tunnels"][0]["tunnel_id"] == "web"       # 数据可读

    r2 = client.post("/api/ingest", json={"results": [
        {"node_id": "intruder", "group": "system", "metrics": []}]},
        headers={"Authorization": "Bearer test-token"})
    assert r2.status_code == 400                         # 非 tailnet 成员拒绝
    assert "intruder" in r2.json()["detail"]["rejected"]
