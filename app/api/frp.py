"""GET /api/frp：frps 隧道 + 链路质量（8899 消费）。"""
from __future__ import annotations

from fastapi import APIRouter

from app import config, constants, db
from app.api import get_aggregator, ok

router = APIRouter()


@router.get("/frp")
async def frp(node: str | None = None):
    agg = get_aggregator()
    payload = agg.latest_for(constants.GROUP_FRP_TRAFFIC, node)
    tunnels = payload.get("tunnels") or []
    serverinfo = payload.get("serverinfo")
    if not tunnels:
        rows = db.get_tunnels(node)
        tunnels = [
            {**r, "cur_conns": None, "last_start_time": None} for r in rows]
    link = payload.get("link") or {}
    # device_map 反查：8899 device_id → tailscale hostname
    host_by_legacy = {v: k for k, v in config.DEVICE_MAP.items()}
    link_out = []
    for dev, d in link.items():
        link_out.append({
            "device_id": dev,
            "hostname": host_by_legacy.get(dev),
            "latency_ms": d.get("latency_ms"),
            "packet_loss_percent": d.get("packet_loss_percent"),
            "frpc_alive": d.get("frpc_alive"),
            "frps_alive": d.get("frps_alive"),
            "bandwidth_up_kbps": d.get("bandwidth_up_kbps"),
            "bandwidth_down_kbps": d.get("bandwidth_down_kbps"),
            "age_seconds": d.get("age_seconds"),
            "display_status": d.get("display_status"),
        })
    return ok({
        "tunnels": tunnels,
        "serverinfo": serverinfo,
        "link": link_out,
        "degraded": bool(payload.get("degraded")),
        "proxy_mode": payload.get("proxy_mode"),
        "source": payload.get("source"),
        "ts": payload.get("ts"),
    })
