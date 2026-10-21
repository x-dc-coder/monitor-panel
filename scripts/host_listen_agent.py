#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
# ─── How to run ───
# python3 scripts/host_listen_agent.py [--once]
#
# Environment variables:
#   INGEST_URL      - monitor-panel base URL (e.g. https://monitor.dc-sy.cn or http://127.0.0.1:8898)
#   INGEST_TOKEN    - Bearer token matching MONITOR_INGEST_TOKEN (empty = idle exit 0)
#   NODE_ID         - Node identifier registered in MONITOR_NODES / tailnet (e.g. "aliyun")
#   HTTP_TIMEOUT    - HTTP request timeout in seconds (default: 10.0)
#   INGEST_INTERVAL - Loop interval in seconds for daemon mode (default: 60.0)
#
# cron 示例（set -a 让 env 文件变量进入本进程环境；缺了它 agent 会静默退出）：
#   * * * * * set -a; . /opt/monitor-panel/agent.env; set +a; \
#     /usr/bin/python3 /opt/monitor-panel/host_listen_agent.py --once \
#     >> /var/log/monitor-host-listen.log 2>&1
#
# Windows 等价采集命令（PowerShell，本 agent 为 Linux 版，Windows 侧可照此实现）：
#   Get-NetTCPConnection -State Listen |
#     Select-Object LocalAddress, LocalPort |
#     ConvertTo-Json -Compress
"""stdlib-only 上报 agent：采集本机监听 TCP 端口（ss -tlnH）并上报 /api/ingest。

监听端口与中心探测互补：agent 报"我在监听"，中心 services 采集器报
"能不能连上"；监听但连不上通常意味着防火墙拦截。
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
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
logger = logging.getLogger("host_listen_agent")


def parse_ss_output(output: str) -> list[dict[str, Any]]:
    """解析 `ss -tlnH` 输出 → [{port, proto, addr}]（按 port 去重）。

    每行：State Recv-Q Send-Q Local-Address:Port Peer-Address:Port
    Local 形如 0.0.0.0:22 / [::]:80 / 127.0.0.53%lo:53 / *:8080。
    """
    by_port: dict[int, dict[str, Any]] = {}
    for line in output.splitlines():
        fields = line.split()
        if len(fields) < 4:
            continue
        local = fields[3]
        if ":" not in local:
            continue
        addr, _, port_s = local.rpartition(":")
        try:
            port = int(port_s)
        except ValueError:
            continue
        if not 0 < port <= 65535:
            continue
        addr = addr.strip("[]") or "*"
        by_port.setdefault(port, {"port": port, "proto": "tcp", "addr": addr})
    return [by_port[p] for p in sorted(by_port)]


def collect_listening_ports(timeout: float) -> list[dict[str, Any]]:
    """执行 ss 并解析；命令失败/输出无法解析时抛 RuntimeError。"""
    cmd = shutil.which("ss") or "/usr/sbin/ss"
    try:
        proc = subprocess.run([cmd, "-tlnH"], capture_output=True, text=True,
                              timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"执行 {cmd} -tlnH 失败: {exc}") from exc
    if proc.returncode != 0:
        raise RuntimeError(
            f"{cmd} -tlnH 退出码 {proc.returncode}: {proc.stderr.strip()}")
    ports = parse_ss_output(proc.stdout)
    if proc.stdout.strip() and not ports:
        raise RuntimeError(f"{cmd} -tlnH 输出无法解析: {proc.stdout[:500]}")
    return ports


def build_results(node_id: str, ports: list[dict[str, Any]],
                  ts: float) -> dict[str, Any]:
    """组装 batch 顶层结构（group=listen_ports）。"""
    return {
        "results": [{
            "node_id": node_id,
            "group": "listen_ports",
            "metrics": [{"name": "listening_count", "value": len(ports), "ts": ts}],
            "payload": {"ports": ports},
        }],
    }


def post_ingest(ingest_url: str, token: str, batch: dict[str, Any],
                timeout: float) -> bool:
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


def run_one_round(ingest_url: str, ingest_token: str, node_id: str,
                  timeout: float) -> bool:
    """执行单轮采集上报。"""
    ts = time.time()
    ports = collect_listening_ports(timeout)
    logger.info("采集到 %d 个监听端口: %s", len(ports),
                ", ".join(str(p["port"]) for p in ports) or "(none)")
    return post_ingest(ingest_url, ingest_token,
                       build_results(node_id, ports, ts), timeout)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="host listening-port ingest agent for monitor-panel")
    parser.add_argument("--once", action="store_true",
                        help="单轮模式（执行一次后退出，cron 适用）")
    args = parser.parse_args()

    ingest_url = os.environ.get("INGEST_URL", "").strip()
    ingest_token = os.environ.get("INGEST_TOKEN", "").strip()
    node_id = os.environ.get("NODE_ID", "aliyun").strip()
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
        logger.info("执行单轮采集上报 (node_id=%s, target=%s)...",
                    node_id, ingest_url)
        try:
            ok = run_one_round(ingest_url, ingest_token, node_id, timeout_val)
        except RuntimeError as exc:
            print(f"监听端口采集失败: {exc}", file=sys.stderr)
            return 1
        return 0 if ok else 1

    logger.info("启动常驻采集循环 (node_id=%s, interval=%.1fs, target=%s)...",
                node_id, interval_val, ingest_url)
    while True:
        try:
            run_one_round(ingest_url, ingest_token, node_id, timeout_val)
        except Exception as exc:  # noqa: BLE001
            logger.exception("常驻循环未知异常: %s", exc)
        time.sleep(interval_val)


if __name__ == "__main__":
    sys.exit(main())
