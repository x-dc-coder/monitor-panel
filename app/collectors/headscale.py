"""headscale 控制面健康采集器（F6）。

- 127.0.0.1:9090/metrics（Prometheus 文本）关键指标：
  headscale_nodestore_nodes / queue_depth / mapresponse_generated_total /
  mapresponse_sent_total（t1 B1 实测：headscale_* 无 per-node 指标，节点状态走 tailscale）
- 127.0.0.1:8080 TCP 探活（控制面是全网单点，必须可见）
- DERP region 999 状态：以控制面可达性代理（无现成 UDP STUN 探针，二期可加深层探测）
"""
from __future__ import annotations

import asyncio
import logging
import re
import time

import httpx

from app import config, constants
from app.collectors.base import Collector, CollectorResult

log = logging.getLogger(__name__)

_METRIC_KEYS = {
    "headscale_nodestore_nodes": "nodes_total",
    "headscale_nodestore_queue_depth": "queue_depth",
    "headscale_mapresponse_generated_total": "mapresponse_generated",
    "headscale_mapresponse_sent_total": "mapresponse_sent",
}
_LINE_RE = re.compile(r"^(\S+)\s+([\d.eE+-]+)")


class HeadscaleCollector(Collector):
    name = "headscale"
    group = constants.GROUP_HEADSCALE
    interval = 30

    # ---------------- mock ----------------
    def _mock_result(self) -> CollectorResult:
        ts = time.time()
        result = CollectorResult(node_id=config.NODE_ID, group=self.group)
        vals = {"nodes_total": 4.0, "queue_depth": 0.0,
                "mapresponse_generated": 1762.0, "mapresponse_sent": 1133.0,
                "tcp_ok": 1.0, "derp_ok": 1.0}
        for k, v in vals.items():
            result.add(k, v, ts)
        result.payload = {"values": vals, "derp_region": config.HEADSCALE_DERP_REGION,
                          "ts": ts, "mock": True}
        return result

    # ---------------- real ----------------
    async def _probe_tcp(self) -> bool:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(config.HEADSCALE_TCP_HOST,
                                        config.HEADSCALE_TCP_PORT),
                timeout=3.0)
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass
            return True
        except Exception:  # noqa: BLE001
            return False

    async def _run_real(self) -> CollectorResult:
        ts = time.time()
        result = CollectorResult(node_id=config.NODE_ID, group=self.group)
        vals: dict[str, float] = {}
        try:
            async with httpx.AsyncClient(timeout=config.STEP_TIMEOUT) as client:
                r = await client.get(config.HEADSCALE_METRICS_URL)
                r.raise_for_status()
            for line in r.text.splitlines():
                m = _LINE_RE.match(line.strip())
                if not m:
                    continue
                name, raw = m.group(1), m.group(2)
                if name in _METRIC_KEYS:
                    vals[_METRIC_KEYS[name]] = float(raw)
        except Exception as exc:  # noqa: BLE001
            log.warning("headscale: metrics unavailable: %s", exc)
        tcp_ok = await self._probe_tcp()
        vals["tcp_ok"] = 1.0 if tcp_ok else 0.0
        vals["derp_ok"] = vals.get("tcp_ok", 0.0)
        if vals:
            for k, v in vals.items():
                result.add(k, v, ts)
        result.payload = {"values": vals, "derp_region": config.HEADSCALE_DERP_REGION,
                          "ts": ts}
        return result

    async def _mock_run(self) -> CollectorResult:
        return self._mock_result()
