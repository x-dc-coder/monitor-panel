"""GET /api/frp：frps 隧道 + 链路质量（8899 消费）。"""
from __future__ import annotations

import time

from fastapi import APIRouter

from app import config, constants, db
from app.api import get_aggregator, ok
from app.api.schemas import ERROR_RESPONSES, FrpEnvelope

router = APIRouter()


@router.get("/frp", responses={200: {"model": FrpEnvelope,
                                    "description": "frps 隧道 + 链路质量"},
                               **ERROR_RESPONSES})
async def frp(node: str | None = None):
    agg = get_aggregator()
    payload = agg.latest_for(constants.GROUP_FRP_TRAFFIC, node)
    tunnels = payload.get("tunnels") or []
    serverinfo = payload.get("serverinfo")
    if not tunnels:
        # dashboard 无隧道（frpc 全离线）或快照缺失：回退 DB 历史行。
        # 行可能很旧（frpc 长期离线）——超龄行标 stale，前端按"已断开"呈现，
        # 避免把 20 天前的状态渲染成在线误导（2026-10-20 面板实录）。
        rows = db.get_tunnels(node)
        now_ts = time.time()
        tunnels = [
            {**r, "cur_conns": None, "last_start_time": None,
             "updated_ts": r.get("updated_ts"),
             "status": "stale" if now_ts - (r.get("updated_ts") or 0) > 900
                       else r.get("status")}
            for r in rows]
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
