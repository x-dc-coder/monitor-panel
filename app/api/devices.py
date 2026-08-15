"""GET /api/devices + POST /api/devices/{id}/ping（只读，限流 5s/设备）。

- device_map：tailscale HostName ↔ 8899 device_id 对照（评审 A 遗漏 1）
- 手动 ping 复用采集器执行器（同一 Semaphore 限并发；E4）
"""
from __future__ import annotations

import logging
import time

from fastapi import APIRouter, HTTPException

from app import config, constants, db
from app.api import get_aggregator, ok
from app.collectors import get as get_collector
from app.collectors.base import CollectorError, validate_ip_or_host

log = logging.getLogger(__name__)
router = APIRouter()

_last_manual_ping: dict[str, float] = {}  # device_id → monotonic


@router.get("/devices")
async def devices(node: str | None = None):
    agg = get_aggregator()
    ts_p = agg.latest.get(constants.GROUP_TS_LATENCY, {})
    pings = ts_p.get("pings", {})
    rows = db.get_devices(node)
    out = []
    for d in rows:
        ping = pings.get(d["device_id"]) or {}
        host = (d.get("name") or "").lower()
        mapped = config.DEVICE_MAP.get(host) or             config.DEVICE_MAP.get(d.get("name") or "")
        status = constants.DEVICE_ONLINE if d.get("online") else constants.DEVICE_OFFLINE
        if d.get("online") and ping.get("timed_out"):
            status = "degraded"
        out.append({
            "device_id": d["device_id"],
            "name": d.get("name"),
            "tailscale_ip": d.get("tailscale_ip"),
            "os": d.get("os"),
            "online": bool(d.get("online")),
            "status": status,
            "relay": d.get("relay"),
            "last_seen_ts": d.get("last_seen_ts"),
            "rx_bytes": d.get("rx_bytes"),
            "tx_bytes": d.get("tx_bytes"),
            "updated_ts": d.get("updated_ts"),
            "latency_ms": ping.get("latency_ms"),
            "latency_min_ms": ping.get("latency_min_ms"),
            "path": ping.get("path"),
            "loss_percent": ping.get("loss_percent"),
            "timed_out": ping.get("timed_out", False),
            "ping_ts": ping.get("ts"),
            "mapped_8899": mapped,
        })
    return ok({"devices": out, "device_map": config.DEVICE_MAP})


@router.post("/devices/{device_id}/ping")
async def ping_device(device_id: str):
    # 限流 5s/设备（评审 E8）
    now = time.monotonic()
    last = _last_manual_ping.get(device_id, 0.0)
    if now - last < config.MANUAL_PING_RATELIMIT_S:
        raise HTTPException(status_code=429,
                            detail="rate limited: 5s per device")
    rows = [d for d in db.get_devices() if d["device_id"] == device_id]
    if not rows:
        raise HTTPException(status_code=404, detail=f"device {device_id} not found")
    dev = rows[0]
    ip = dev.get("tailscale_ip")
    if not ip or not validate_ip_or_host(ip):
        raise HTTPException(status_code=400, detail=f"invalid tailscale ip {ip!r}")
    collector = get_collector("tailscale")
    if collector is None or collector.mock:
        # mock 模式：返回确定性模拟结果（不真跑命令）
        import hashlib
        seed = int(hashlib.md5(device_id.encode()).hexdigest()[:6], 16)
        latency = 15 + seed % 80
        _last_manual_ping[device_id] = now
        return ok({"device_id": device_id, "tailscale_ip": ip,
                   "latency_ms": float(latency), "path": "derp",
                   "loss_percent": 0.0, "timed_out": False, "mock": True})
    _last_manual_ping[device_id] = now
    try:
        ping = await collector.ping_device(device_id, ip)
    except CollectorError as exc:
        log.warning("manual ping %s failed: %s", device_id, exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return ok(ping)
