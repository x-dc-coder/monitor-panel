#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
# ─── How to run ───
# python3 scripts/ingest_agent.py [--once]
#
# Environment variables:
#   INGEST_URL    - monitor-panel base URL (e.g. https://monitor.dc-sy.cn or http://127.0.0.1:8898)
#   INGEST_TOKEN  - Bearer token matching MONITOR_INGEST_TOKEN
#   NODE_ID       - Node identifier registered in MONITOR_NODES (e.g. "aliyun")
#   FRPS_URL      - Local frps dashboard URL (default: http://127.0.0.1:7400)
#   FRPS_USER     - frps dashboard basic auth user (default: "")
#   FRPS_PASS     - frps dashboard basic auth password (default: "")
#   HTTP_TIMEOUT  - HTTP request timeout in seconds (default: 10.0)
#   INGEST_INTERVAL - Loop interval in seconds for daemon mode (default: 60.0)
"""stdlib-only 上报 agent：采集本机 frps dashboard 流量与隧道并上报至 monitor-panel /api/ingest。"""
from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("ingest_agent")

PROXY_TYPES: tuple[str, ...] = ("tcp", "http", "https", "udp")


def _get_float_metric(val: Any) -> float:
    """安全转换为 float，缺失或非数值回退 0.0。"""
    if val is None:
        return 0.0
    try:
        return float(val)
    except (TypeError, ValueError):
        return 0.0


def build_metrics(
    serverinfo: dict[str, Any] | None,
    ts: float,
    proxy_count: int | None = None,
) -> list[dict[str, Any]]:
    """将 serverinfo 映射为白名单五指标（frp_traffic）。

    白名单五指标：
    - total_traffic_in
    - total_traffic_out
    - cur_conns
    - client_count
    - proxy_count
    """
    info = serverinfo or {}

    total_in = _get_float_metric(
        info.get("total_traffic_in", info.get("totalTrafficIn")),
    )
    total_out = _get_float_metric(
        info.get("total_traffic_out", info.get("totalTrafficOut")),
    )
    cur_conns = _get_float_metric(
        info.get("cur_conns", info.get("curConns")),
    )
    client_count = _get_float_metric(
        info.get("client_count", info.get("clientCounts")),
    )

    if proxy_count is not None:
        p_count = float(proxy_count)
    else:
        # 支持 proxy_count、proxyNumber 或 proxy_type_count 累加
        raw_count = info.get("proxy_count", info.get("proxyNumber"))
        if raw_count is not None:
            p_count = _get_float_metric(raw_count)
        else:
            type_counts = info.get("proxy_type_count") or info.get("proxyTypeCount")
            if isinstance(type_counts, dict):
                p_count = float(sum(_get_float_metric(v) for v in type_counts.values()))
            else:
                p_count = 0.0

    return [
        {"name": "total_traffic_in", "value": total_in, "ts": ts},
        {"name": "total_traffic_out", "value": total_out, "ts": ts},
        {"name": "cur_conns", "value": cur_conns, "ts": ts},
        {"name": "client_count", "value": client_count, "ts": ts},
        {"name": "proxy_count", "value": p_count, "ts": ts},
    ]


def build_tunnels(proxies_by_type: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """将各类型代理列表规整为 tunnels 行。

    字段契约：
    tunnel_id: proxy name
    type: 代理类型 (tcp/http/https/udp 等)
    remote_port: 远程暴露端口（若有）
    local_addr: local_ip:local_port 或 remote_port 回退或 None
    status: status 字符串，默认 "unknown"
    traffic_in: today_traffic_in 或 todayTrafficIn
    traffic_out: today_traffic_out 或 todayTrafficOut
    """
    tunnels: list[dict[str, Any]] = []

    for ptype, items in proxies_by_type.items():
        if not isinstance(items, list):
            continue
        for p in items:
            if not isinstance(p, dict):
                continue
            conf = p.get("conf") if isinstance(p.get("conf"), dict) else {}
            tunnel_id = p.get("name") or conf.get("name") or "unknown"
            t_type = conf.get("type") or p.get("type") or ptype
            remote_port = conf.get("remote_port", conf.get("remotePort", p.get("remote_port")))

            local_ip = conf.get("local_ip", conf.get("localIP"))
            local_port = conf.get("local_port", conf.get("localPort"))
            if local_ip is not None and local_port is not None:
                local_addr: str | None = f"{local_ip}:{local_port}"
            elif local_ip is not None:
                local_addr = str(local_ip)
            elif remote_port is not None:
                local_addr = str(remote_port)
            else:
                local_addr = None

            t_in = p.get("today_traffic_in", p.get("todayTrafficIn", 0)) or 0
            t_out = p.get("today_traffic_out", p.get("todayTrafficOut", 0)) or 0

            tunnels.append({
                "tunnel_id": tunnel_id,
                "type": t_type,
                "remote_port": remote_port,
                "local_addr": local_addr,
                "status": p.get("status", "unknown"),
                "traffic_in": int(t_in),
                "traffic_out": int(t_out),
            })

    return tunnels


def build_results(
    node_id: str,
    serverinfo: dict[str, Any] | None,
    tunnels: list[dict[str, Any]],
    ts: float,
) -> dict[str, Any]:
    """组装 batch 顶层结构。"""
    metrics = build_metrics(serverinfo, ts, proxy_count=len(tunnels))
    payload: dict[str, Any] = {
        "source": "frps-dashboard",
        "ts": ts,
        "serverinfo": serverinfo,
        "tunnels": tunnels,
    }
    return {
        "results": [
            {
                "node_id": node_id,
                "group": "frp_traffic",
                "metrics": metrics,
                "payload": payload,
            },
        ],
    }


def _http_get_json(
    url: str,
    user: str,
    password: str,
    timeout: float,
) -> Any:
    """使用 stdlib urllib 发起 HTTP GET 请求获取 JSON。"""
    req = urllib.request.Request(url)
    if user or password:
        token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
        req.add_header("Authorization", f"Basic {token}")

    with urllib.request.urlopen(req, timeout=timeout) as resp:
        charset = resp.headers.get_content_charset() or "utf-8"
        raw = resp.read().decode(charset)
        return json.loads(raw)


def fetch_frps_data(
    frps_url: str,
    user: str,
    password: str,
    timeout: float,
) -> tuple[dict[str, Any] | None, dict[str, list[dict[str, Any]]]]:
    """从 frps dashboard 抓取 serverinfo 与各类型代理。"""
    base = frps_url.rstrip("/")
    serverinfo: dict[str, Any] | None = None
    try:
        data = _http_get_json(f"{base}/api/serverinfo", user, password, timeout)
        if isinstance(data, dict):
            serverinfo = data
    except Exception as exc:  # noqa: BLE001
        logger.warning("抓取 serverinfo 失败 (%s): %s", f"{base}/api/serverinfo", exc)

    proxies_by_type: dict[str, list[dict[str, Any]]] = {}
    for ptype in PROXY_TYPES:
        try:
            p_data = _http_get_json(f"{base}/api/proxy/{ptype}", user, password, timeout)
            if isinstance(p_data, dict) and "proxies" in p_data and isinstance(p_data["proxies"], list):
                proxies_by_type[ptype] = p_data["proxies"]
            elif isinstance(p_data, list):
                proxies_by_type[ptype] = p_data
            else:
                proxies_by_type[ptype] = []
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                proxies_by_type[ptype] = []
            else:
                logger.warning("抓取 proxy/%s 失败 (HTTP %d): %s", ptype, exc.code, exc)
                proxies_by_type[ptype] = []
        except Exception as exc:  # noqa: BLE001
            logger.warning("抓取 proxy/%s 异常: %s", ptype, exc)
            proxies_by_type[ptype] = []

    return serverinfo, proxies_by_type


def post_ingest(
    ingest_url: str,
    token: str,
    batch: dict[str, Any],
    timeout: float,
) -> bool:
    """POST batch 到 /api/ingest。返回 True 表示成功。"""
    url = f"{ingest_url.rstrip('/')}/api/ingest"
    body = json.dumps(batch).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            code = resp.status
            resp_body = resp.read().decode("utf-8", errors="replace")
            logger.info("上报成功 (HTTP %d): %s", code, resp_body[:200])
            return True
    except urllib.error.HTTPError as exc:
        err_msg = exc.read().decode("utf-8", errors="replace")
        if exc.code == 400:
            logger.error("【400 拒绝】节点未在 MONITOR_NODES 注册或请求体非法: %s", err_msg)
        elif exc.code == 401:
            logger.error("【401 认证失败】INGEST_TOKEN 未配置或无效: %s", err_msg)
        elif exc.code == 404:
            logger.error("【404 未启用】面板端 MONITOR_INGEST_ENABLED 未开启: %s", err_msg)
        else:
            logger.error("上报失败 HTTP %d: %s", exc.code, err_msg)
        return False
    except Exception as exc:  # noqa: BLE001
        logger.error("上报网络异常 (%s): %s", url, exc)
        return False


def run_one_round(
    ingest_url: str,
    ingest_token: str,
    node_id: str,
    frps_url: str,
    frps_user: str,
    frps_pass: str,
    timeout: float,
) -> bool:
    """执行单轮采集上报。"""
    ts = time.time()
    serverinfo, proxies_by_type = fetch_frps_data(frps_url, frps_user, frps_pass, timeout)
    tunnels = build_tunnels(proxies_by_type)
    batch = build_results(node_id, serverinfo, tunnels, ts)
    return post_ingest(ingest_url, ingest_token, batch, timeout)


def main() -> int:  # noqa: BROAD_EXCEPT_OK
    parser = argparse.ArgumentParser(description="frps ingest agent for monitor-panel")
    parser.add_argument("--once", action="store_true", help="单轮模式（执行一次后退出，cron 适用）")
    args = parser.parse_args()

    ingest_url = os.environ.get("INGEST_URL", "").strip()
    ingest_token = os.environ.get("INGEST_TOKEN", "").strip()
    node_id = os.environ.get("NODE_ID", "aliyun").strip()
    frps_url = os.environ.get("FRPS_URL", "http://127.0.0.1:7400").strip()
    frps_user = os.environ.get("FRPS_USER", "").strip()
    frps_pass = os.environ.get("FRPS_PASS", "").strip()

    timeout_val = float(os.environ.get("HTTP_TIMEOUT", "10.0"))
    interval_val = float(os.environ.get("INGEST_INTERVAL", "60.0"))

    # INGEST_TOKEN 为空时直接 log + exit 0（未配置态静默）
    if not ingest_token:
        logger.info("INGEST_TOKEN 未配置，agent 处于未配置态，静默退出")
        return 0

    if not ingest_url:
        logger.error("INGEST_URL 未配置")
        return 1

    if args.once:
        logger.info("执行单轮采集上报 (node_id=%s, target=%s)...", node_id, ingest_url)
        ok = run_one_round(
            ingest_url=ingest_url,
            ingest_token=ingest_token,
            node_id=node_id,
            frps_url=frps_url,
            frps_user=frps_user,
            frps_pass=frps_pass,
            timeout=timeout_val,
        )
        return 0 if ok else 1

    logger.info("启动常驻采集循环 (node_id=%s, interval=%.1fs, target=%s)...", node_id, interval_val, ingest_url)
    while True:
        try:
            run_one_round(
                ingest_url=ingest_url,
                ingest_token=ingest_token,
                node_id=node_id,
                frps_url=frps_url,
                frps_user=frps_user,
                frps_pass=frps_pass,
                timeout=timeout_val,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("常驻循环未知异常: %s", exc)
        time.sleep(interval_val)


if __name__ == "__main__":
    sys.exit(main())
