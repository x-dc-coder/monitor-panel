"""公网探测对采集器（F4/F7）。

- 探测对来自 config.PROBES（(src_node, dst, kind) → 加节点 = 加一行配置）
- 四种探测：ICMP（/bin/ping 经 sudoers 白名单）、TCP（asyncio 连接）、
  HTTP（httpx）、DNS（getaddrinfo）；asyncio 并发 + 每项硬超时
- 本期 cloud-1 单向探测（含 cloud2-443 预留对等探测位）
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time

from app import config, constants
from app.collectors.base import Collector, CollectorError, CollectorResult, run_cmd

log = logging.getLogger(__name__)

_ICMP_AVG_RE = re.compile(r"=s*([d.]+)/([d.]+)/([d.]+)")
_ICMP_LOSS_RE = re.compile(r"([d.]+)%s+packet loss")


class NetworkCollector(Collector):
    name = "network"
    group = constants.GROUP_NETWORK
    interval = 60

    def __init__(self, mock: bool | None = None) -> None:
        super().__init__(mock)
        self._probes = config.PROBES
        self._last: dict[str, dict] = {}

    # ---------------- mock（按探测名确定性生成，测试稳定） ----------------
    def _mock_result(self) -> CollectorResult:
        ts = time.time()
        result = CollectorResult(node_id=config.NODE_ID, group=self.group)
        rows = []
        for p in self._probes:
            seed = int(hashlib.md5(p["name"].encode()).hexdigest()[:6], 16)
            latency = 8 + (seed % 70) + (seed % 13) * 0.3
            reachable = p.get("reachable", True)
            row = {"name": p["name"], "kind": p["kind"], "target": p["target"],
                   "port": p.get("port"), "latency_ms": round(latency, 1),
                   "loss_percent": 0.0, "reachable": 1 if reachable else 0,
                   "ts": ts, "mock": True}
            rows.append(row)
            result.add(f"latency_ms.{p['name']}", latency, ts)
            result.add(f"loss_percent.{p['name']}", 0.0, ts)
            result.add(f"reachable.{p['name']}", 1.0 if reachable else 0.0, ts)
        self._last = {r["name"]: r for r in rows}
        result.payload = {"probes": rows, "ts": ts, "mock": True}
        return result

    # ---------------- real ----------------
    async def _probe_icmp(self, p: dict) -> dict:
        started = time.monotonic()
        try:
            rc, out, err = await run_cmd(
                [config.PING_CMD, "-c", "4", "-W", "2", p["target"]],
                sudo=config.SUDO, timeout=config.STEP_TIMEOUT)
            if rc == 0 and "packet loss" in out:
                m = _ICMP_AVG_RE.search(out)
                loss_m = _ICMP_LOSS_RE.search(out)
                loss = float(loss_m.group(1)) if loss_m else 0.0
                latency = float(m.group(2)) if m else None  # avg
                return {"name": p["name"], "kind": "icmp", "target": p["target"],
                        "latency_ms": round(latency, 1) if latency else None,
                        "loss_percent": loss, "reachable": loss < 100,
                        "elapsed_ms": round((time.monotonic() - started) * 1000, 1)}
            return {"name": p["name"], "kind": "icmp", "target": p["target"],
                    "latency_ms": None, "loss_percent": 100.0, "reachable": False,
                    "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
                    "error": (err or out).strip()[:120]}
        except CollectorError as exc:
            return {"name": p["name"], "kind": "icmp", "target": p["target"],
                    "latency_ms": None, "loss_percent": 100.0, "reachable": False,
                    "error": str(exc)[:120]}

    async def _probe_tcp(self, p: dict) -> dict:
        port = int(p.get("port", 443))
        started = time.monotonic()
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(p["target"], port), timeout=8.0)
            latency = (time.monotonic() - started) * 1000
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass
            return {"name": p["name"], "kind": "tcp", "target": p["target"],
                    "port": port, "latency_ms": round(latency, 1), "loss_percent": 0.0,
                    "reachable": True}
        except Exception as exc:  # noqa: BLE001
            return {"name": p["name"], "kind": "tcp", "target": p["target"],
                    "port": port, "latency_ms": None, "loss_percent": 100.0,
                    "reachable": False, "error": str(exc)[:120]}

    async def _probe_http(self, p: dict) -> dict:
        import httpx
        started = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as client:
                r = await client.get(p["target"])
            latency = (time.monotonic() - started) * 1000
            return {"name": p["name"], "kind": "http", "target": p["target"],
                    "latency_ms": round(latency, 1), "loss_percent": 0.0,
                    "reachable": r.status_code < 500, "status_code": r.status_code}
        except Exception as exc:  # noqa: BLE001
            return {"name": p["name"], "kind": "http", "target": p["target"],
                    "latency_ms": None, "loss_percent": 100.0, "reachable": False,
                    "error": str(exc)[:120]}

    async def _probe_dns(self, p: dict) -> dict:
        started = time.monotonic()
        try:
            infos = await asyncio.wait_for(
                asyncio.getaddrinfo(p["target"], 443), timeout=8.0)
            latency = (time.monotonic() - started) * 1000
            return {"name": p["name"], "kind": "dns", "target": p["target"],
                    "latency_ms": round(latency, 1), "loss_percent": 0.0,
                    "reachable": bool(infos)}
        except Exception as exc:  # noqa: BLE001
            return {"name": p["name"], "kind": "dns", "target": p["target"],
                    "latency_ms": None, "loss_percent": 100.0, "reachable": False,
                    "error": str(exc)[:120]}

    async def _run_real(self) -> CollectorResult:
        ts = time.time()
        result = CollectorResult(node_id=config.NODE_ID, group=self.group)
        handlers = {"icmp": self._probe_icmp, "tcp": self._probe_tcp,
                    "http": self._probe_http, "dns": self._probe_dns}
        rows = await asyncio.gather(*[
            handlers[p["kind"]](p) for p in self._probes
            if p["kind"] in handlers], return_exceptions=True)
        norm = []
        for r in rows:
            if isinstance(r, Exception):
                log.warning("network: probe failed: %s", r)
                continue
            norm.append(r)
            result.add(f"latency_ms.{r['name']}", r["latency_ms"] or 0.0, ts)
            result.add(f"loss_percent.{r['name']}", r["loss_percent"], ts)
            result.add(f"reachable.{r['name']}", 1.0 if r["reachable"] else 0.0, ts)
        self._last = {r["name"]: r for r in norm}
        result.payload = {"probes": norm, "ts": ts}
        return result

    async def run_once(self) -> CollectorResult:
        if self.mock:
            return self._mock_result()
        return await self._run_real()
