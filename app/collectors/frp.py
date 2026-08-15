"""frp 采集器（F1）。

- frps dashboard API（127.0.0.1:7500，basic auth）：
  /api/serverinfo + per-type 端点 /api/proxy/{tcp,udp,http,https,stcp,xtcp}
  （t1 B5 实测；/api/proxy/all 先探测一次，存在则用之，否则聚合 per-type——评审 F2/E5）
- 补充消费 8899 /api/status（Bearer token）的 latency_ms / packet_loss_percent /
  frpc_alive / frps_alive / 带宽（评审 F3），并入 samples（group=frp_link）
- 降级：dashboard 不可用时仅保留 8899 agent 链路数据，不中断整轮
"""
from __future__ import annotations

import asyncio
import logging
import time

import httpx

from app import config, constants
from app.collectors.base import Collector, CollectorResult, CollectorError

log = logging.getLogger(__name__)

PROXY_TYPES = ("tcp", "udp", "http", "https", "stcp", "xtcp")

# 8899 /api/status 响应字段 → 指标名
_LINK_METRICS = (
    ("latency_ms", "latency_ms"),
    ("packet_loss_percent", "packet_loss_percent"),
    ("frpc_alive", "frpc_alive"),
    ("frps_alive", "frps_alive"),
    ("bandwidth_up_kbps", "bandwidth_up_kbps"),
    ("bandwidth_down_kbps", "bandwidth_down_kbps"),
)


class FrpCollector(Collector):
    name = "frp"
    group = constants.GROUP_FRP_TRAFFIC
    interval = 30

    def __init__(self, mock: bool | None = None) -> None:
        super().__init__(mock)
        self._use_proxy_all: bool | None = None  # 探测结果缓存

    # ---------------- mock ----------------
    def _mock_result(self) -> CollectorResult:
        ts = time.time()
        result = CollectorResult(node_id=config.NODE_ID, group=self.group)
        tunnels = [
            {"tunnel_id": "desktop-rdp", "type": "tcp", "remote_port": 20000,
             "local_addr": "192.168.1.20:3389", "status": "online",
             "traffic_in": 8_500_000, "traffic_out": 240_000_000, "cur_conns": 1},
            {"tunnel_id": "desktop-ssh", "type": "tcp", "remote_port": 20001,
             "local_addr": "192.168.1.20:22", "status": "online",
             "traffic_in": 320_000, "traffic_out": 1_100_000, "cur_conns": 2},
            {"tunnel_id": "camera-web", "type": "tcp", "remote_port": 20002,
             "local_addr": "192.168.1.30:80", "status": "online",
             "traffic_in": 12_000_000, "traffic_out": 90_000_000, "cur_conns": 3},
        ]
        serverinfo = {
            "version": "0.70.1", "bind_port": 7000, "cur_conns": 1,
            "client_count": 1, "total_traffic_in": 30_588_355,
            "total_traffic_out": 830_594_701, "proxy_type_count": {"tcp": 3},
        }
        result.payload = {"serverinfo": serverinfo, "tunnels": tunnels,
                          "source": "mock", "ts": ts}
        result.add("total_traffic_in", serverinfo["total_traffic_in"], ts)
        result.add("total_traffic_out", serverinfo["total_traffic_out"], ts)
        result.add("cur_conns", serverinfo["cur_conns"], ts)
        result.add("client_count", serverinfo["client_count"], ts)
        result.add("proxy_count", len(tunnels), ts)
        links = self._mock_link_metrics(ts)
        result.groups[constants.GROUP_FRP_LINK] = links
        result.payload["link"] = self._last_links
        return result

    def _mock_link_metrics(self, ts: float):
        """模拟 8899 /api/status：cloud-server / desktop / laptop 三设备。"""
        links = {
            "cloud-server": {"latency_ms": 8.9, "packet_loss_percent": 0.0,
                             "frpc_alive": 1, "frps_alive": 1,
                             "bandwidth_up_kbps": 1024, "bandwidth_down_kbps": 896,
                             "kind": "cloud-server", "age_seconds": 12},
            "desktop": {"latency_ms": 27.3, "packet_loss_percent": 0.4,
                        "frpc_alive": 1, "frps_alive": 1,
                        "bandwidth_up_kbps": 210, "bandwidth_down_kbps": 480,
                        "kind": "desktop", "age_seconds": 18},
            "laptop": {"latency_ms": 41.6, "packet_loss_percent": 1.1,
                       "frpc_alive": 1, "frps_alive": 1,
                       "bandwidth_up_kbps": 90, "bandwidth_down_kbps": 150,
                       "kind": "laptop", "age_seconds": 5},
        }
        metrics = []
        for dev, d in links.items():
            for src_field, metric_base in _LINK_METRICS:
                if src_field in d and d[src_field] is not None:
                    metrics.append((f"{metric_base}.{dev}", float(d[src_field]), ts))
        self._last_links = links
        return metrics

    # ---------------- real ----------------
    async def _fetch_serverinfo(self, client: httpx.AsyncClient) -> dict:
        resp = await client.get("/api/serverinfo")
        resp.raise_for_status()
        info = resp.json()
        return {
            "version": info.get("version"), "bind_port": info.get("bindPort"),
            "cur_conns": info.get("curConns"), "client_count": info.get("clientCounts"),
            "total_traffic_in": info.get("totalTrafficIn"),
            "total_traffic_out": info.get("totalTrafficOut"),
            "allow_ports": info.get("allowPortsStr"),
            "proxy_type_count": info.get("proxyTypeCount"),
        }

    async def _fetch_tunnels(self, client: httpx.AsyncClient) -> tuple[list[dict], str]:
        """/api/proxy/all 先探测一次；返回空/404/400/405 → per-type 聚合。

        frp 0.70 实测：/api/proxy/all 返回 200 但 proxies 恒为空（端点失效），
        必须用 per-type 端点（评审 F2/E5）。
        """
        if self._use_proxy_all is None:
            try:
                r = await client.get("/api/proxy/all", timeout=3)
                if r.status_code == 200:
                    data = r.json()
                    self._use_proxy_all = bool(data.get("proxies"))
                else:
                    self._use_proxy_all = False
            except Exception:  # noqa: BLE001
                self._use_proxy_all = False
        tunnels: list[dict] = []
        if self._use_proxy_all:
            r = await client.get("/api/proxy/all")
            r.raise_for_status()
            proxies = r.json().get("proxies", [])
            for p in proxies:
                conf = p.get("conf", {})
                tunnels.append(self._norm_tunnel(p, conf))
            return tunnels, "all"
        for ptype in PROXY_TYPES:
            r = await client.get(f"/api/proxy/{ptype}")
            if r.status_code != 200:
                continue
            for p in r.json().get("proxies", []):
                conf = p.get("conf", {})
                tunnels.append(self._norm_tunnel(p, conf))
        return tunnels, "per-type"

    @staticmethod
    def _norm_tunnel(p: dict, conf: dict) -> dict:
        return {
            "tunnel_id": p.get("name"),
            "type": conf.get("type", p.get("type")),
            "remote_port": conf.get("remotePort"),
            "local_addr": f"{conf.get('localIP', '')}:{conf.get('localPort', '')}"
                          if conf.get("localIP") else None,
            "status": p.get("status", "unknown"),
            "traffic_in": p.get("todayTrafficIn", 0) or 0,
            "traffic_out": p.get("todayTrafficOut", 0) or 0,
            "cur_conns": p.get("curConns", 0),
            "last_start_time": p.get("lastStartTime"),
        }

    async def _fetch_legacy_links(self) -> dict | None:
        """8899 /api/status（Bearer token）。失败降级返回 None。"""
        if not config.LEGACY_TOKEN or not config.LEGACY_STATUS_URL:
            return None
        headers = {"Authorization": f"Bearer {config.LEGACY_TOKEN}"}
        try:
            async with httpx.AsyncClient(timeout=config.STEP_TIMEOUT) as client:
                r = await client.get(config.LEGACY_STATUS_URL, headers=headers)
                r.raise_for_status()
                data = r.json()
                devices = data.get("devices") or data.get("status") or data
                if isinstance(devices, dict) and "devices" in data:
                    devices = data["devices"]
                if isinstance(devices, dict):
                    return devices
                return {d.get("device_id"): d for d in devices
                        if isinstance(d, dict) and d.get("device_id")}
        except Exception as exc:  # noqa: BLE001
            log.warning("frp: 8899 /api/status unavailable: %s", exc)
            return None

    async def _run_real(self) -> CollectorResult:
        ts = time.time()
        result = CollectorResult(node_id=config.NODE_ID, group=self.group)
        result.payload = {"source": "frps-dashboard", "ts": ts,
                          "serverinfo": None, "tunnels": [], "link": {},
                          "degraded": False}
        serverinfo: dict | None = None
        tunnels: list[dict] = []
        try:
            auth = (config.FRPS_USER, config.FRPS_PASS)
            async with httpx.AsyncClient(base_url=config.FRPS_URL,
                                         auth=auth, timeout=config.FRPS_TIMEOUT) as client:
                serverinfo = await asyncio.wait_for(
                    self._fetch_serverinfo(client), timeout=config.STEP_TIMEOUT)
                tunnels, mode = await asyncio.wait_for(
                    self._fetch_tunnels(client), timeout=config.STEP_TIMEOUT)
                result.payload["proxy_mode"] = mode
        except Exception as exc:  # noqa: BLE001 —— 降级：仅保留 8899 agent 数据（评审 E5）
            result.payload["degraded"] = True
            result.payload["error"] = str(exc)[:200]
            log.warning("frp: dashboard unavailable (%s); degraded to 8899 agent data", exc)

        if serverinfo:
            result.payload["serverinfo"] = serverinfo
            for src_field, metric_base in (
                    ("total_traffic_in", "total_traffic_in"),
                    ("total_traffic_out", "total_traffic_out"),
                    ("cur_conns", "cur_conns"),
                    ("client_count", "client_count")):
                v = serverinfo.get(src_field)
                if v is not None:
                    result.add(metric_base, float(v), ts)
            result.add("proxy_count", float(len(tunnels)), ts)
        if tunnels:
            result.payload["tunnels"] = tunnels

        # 8899 链路质量（frp_link group，并入 samples —— 评审 F3）
        links = await self._fetch_legacy_links()
        if links:
            result.payload["link"] = links
            metrics: list[tuple] = []
            for dev, d in links.items():
                for src_field, metric_base in _LINK_METRICS:
                    v = d.get(src_field)
                    if v is not None:
                        metrics.append((f"{metric_base}.{dev}", float(v), ts))
            result.groups[constants.GROUP_FRP_LINK] = metrics
        return result

    async def _mock_run(self) -> CollectorResult:
        return self._mock_result()
