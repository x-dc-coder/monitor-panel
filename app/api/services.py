"""GET /api/services：服务台账（自动发现 + 探活状态）。

- 数据权威源：db.services 表（collector 每轮 upsert）
- 汇总 open/closed/manual 计数 + 约定区信息
"""
from __future__ import annotations

from fastapi import APIRouter

from app import config, constants, db
from app.api import ok
from app.api.schemas import ERROR_RESPONSES, ServicesEnvelope

router = APIRouter()


@router.get("/services", responses={200: {"model": ServicesEnvelope,
                                           "description": "服务台账"},
                                    **ERROR_RESPONSES})
async def services(node: str | None = None):
    rows = db.get_services(node_id=node or config.NODE_ID)
    out = []
    for r in rows:
        ip = r.get("tailscale_ip") or ""
        port = r.get("port")
        public_url = config.SERVICES_PUBLIC_URLS.get(f"{ip}:{port}")
        out.append({
            "device_id": r.get("device_id"),
            "device_name": r.get("device_name"),
            "tailscale_ip": ip,
            "port": port,
            "name": r.get("name") or r.get("auto_name"),
            "auto_name": r.get("auto_name"),
            "protocol": r.get("protocol"),
            "url": r.get("url"),
            "public_url": public_url,
            "http_status": r.get("http_status"),
            "latency_ms": r.get("latency_ms"),
            "status": r.get("status"),
            "missing_rounds": r.get("missing_rounds"),
            "source": r.get("source"),
            "enabled": bool(r.get("enabled")),
            "first_seen_ts": r.get("first_seen_ts"),
            "last_seen_ts": r.get("last_seen_ts"),
            "updated_ts": r.get("updated_ts"),
        })
    return ok({
        "services": out,
        "range": list(config.SERVICES_PORT_RANGE),
        "summary": {
            "total": len(out),
            "open": sum(1 for r in out if r["status"] == "open"),
            "closed": sum(1 for r in out if r["status"] == "closed"),
            "manual": sum(1 for r in out if r["source"] == "manual"),
        },
    })
