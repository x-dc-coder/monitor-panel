"""tests/test_ingest_agent.py: 测试 scripts/ingest_agent.py 纯函数。"""
from __future__ import annotations

import importlib.util
from pathlib import Path

from app import constants

# 动态加载 scripts/ingest_agent.py 模块
_script_path = Path(__file__).parent.parent / "scripts" / "ingest_agent.py"
_spec = importlib.util.spec_from_file_location("ingest_agent", _script_path)
assert _spec is not None and _spec.loader is not None
_agent = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_agent)

build_metrics = _agent.build_metrics
build_tunnels = _agent.build_tunnels
build_results = _agent.build_results


def test_build_metrics_whitelist_and_mapping():
    """断言 5 个白名单指标名与数值映射，以及字段容错。"""
    ts = 1770000000.0
    whitelist = constants.GROUP_METRICS[constants.GROUP_FRP_TRAFFIC]
    assert set(whitelist) == {
        "total_traffic_in",
        "total_traffic_out",
        "cur_conns",
        "client_count",
        "proxy_count",
    }

    # 1. 模拟标准 serverinfo (含 camelCase 与 snake_case 兼容)
    serverinfo = {
        "version": "0.63.0",
        "total_traffic_in": 123456,
        "total_traffic_out": 654321,
        "cur_conns": 5,
        "client_count": 2,
        "proxy_count": 8,
    }
    metrics = build_metrics(serverinfo, ts)
    assert len(metrics) == 5

    metric_map = {m["name"]: m["value"] for m in metrics}
    for m in metrics:
        assert m["name"] in whitelist
        assert m["ts"] == ts

    assert metric_map["total_traffic_in"] == 123456.0
    assert metric_map["total_traffic_out"] == 654321.0
    assert metric_map["cur_conns"] == 5.0
    assert metric_map["client_count"] == 2.0
    assert metric_map["proxy_count"] == 8.0

    # 2. 容错测试：None 或空字典全部回退 0.0
    metrics_empty = build_metrics(None, ts)
    for m in metrics_empty:
        assert m["name"] in whitelist
        assert m["value"] == 0.0

    # 3. 兼容 camelCase (totalTrafficIn 等) 与 proxy_count 显式覆盖
    serverinfo_camel = {
        "totalTrafficIn": 999,
        "totalTrafficOut": 888,
        "curConns": 3,
        "clientCounts": 1,
    }
    metrics_camel = build_metrics(serverinfo_camel, ts, proxy_count=4)
    camel_map = {m["name"]: m["value"] for m in metrics_camel}
    assert camel_map["total_traffic_in"] == 999.0
    assert camel_map["total_traffic_out"] == 888.0
    assert camel_map["cur_conns"] == 3.0
    assert camel_map["client_count"] == 1.0
    assert camel_map["proxy_count"] == 4.0


def test_build_tunnels_mapping_and_fallbacks():
    """断言 per-type 代理 → tunnels 行映射（含缺字段回退）。"""
    proxies_by_type = {
        "tcp": [
            {
                "name": "ssh-tunnel",
                "type": "tcp",
                "status": "online",
                "today_traffic_in": 1024,
                "today_traffic_out": 2048,
                "conf": {
                    "remote_port": 2222,
                    "local_ip": "127.0.0.1",
                    "local_port": 22,
                },
            },
            {
                "name": "fallback-tunnel",
                "status": "offline",
                "conf": {
                    "remotePort": 8080,
                },
            },
        ],
        "http": [
            {
                "name": "web-tunnel",
                "todayTrafficIn": 5000,
                "todayTrafficOut": 10000,
                "conf": {
                    "type": "http",
                    "localIP": "192.168.1.100",
                    "localPort": 80,
                },
            },
        ],
        "udp": [],
    }

    tunnels = build_tunnels(proxies_by_type)
    assert len(tunnels) == 3

    t_map = {t["tunnel_id"]: t for t in tunnels}

    # 1. 完整字段
    ssh_t = t_map["ssh-tunnel"]
    assert ssh_t["type"] == "tcp"
    assert ssh_t["remote_port"] == 2222
    assert ssh_t["local_addr"] == "127.0.0.1:22"
    assert ssh_t["status"] == "online"
    assert ssh_t["traffic_in"] == 1024
    assert ssh_t["traffic_out"] == 2048

    # 2. 缺 local_addr 回退 remote_port
    fb_t = t_map["fallback-tunnel"]
    assert fb_t["type"] == "tcp"  # 来自 ptype
    assert fb_t["remote_port"] == 8080
    assert fb_t["local_addr"] == "8080"
    assert fb_t["status"] == "offline"
    assert fb_t["traffic_in"] == 0
    assert fb_t["traffic_out"] == 0

    # 3. camelCase
    web_t = t_map["web-tunnel"]
    assert web_t["type"] == "http"
    assert web_t["remote_port"] is None
    assert web_t["local_addr"] == "192.168.1.100:80"
    assert web_t["status"] == "unknown"
    assert web_t["traffic_in"] == 5000
    assert web_t["traffic_out"] == 10000


def test_build_results_structure():
    """断言 batch 顶层形态 {results:[{node_id,group,metrics,payload}]}。"""
    ts = 1770001111.0
    serverinfo = {"cur_conns": 1}
    tunnels = [{"tunnel_id": "t1", "type": "tcp", "status": "online"}]

    batch = build_results("aliyun", serverinfo, tunnels, ts)

    assert "results" in batch
    results = batch["results"]
    assert len(results) == 1

    item = results[0]
    assert item["node_id"] == "aliyun"
    assert item["group"] == "frp_traffic"
    assert isinstance(item["metrics"], list)
    assert len(item["metrics"]) == 5

    payload = item["payload"]
    assert payload["source"] == "frps-dashboard"
    assert payload["ts"] == ts
    assert payload["serverinfo"] == serverinfo
    assert payload["tunnels"] == tunnels
