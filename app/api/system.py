"""GET /api/system /api/certs /api/network：资源/证书/探测（读快照）。"""
from __future__ import annotations

from fastapi import APIRouter, Query

from app import config, constants, db
from app.api import get_aggregator, ok
from app.api.schemas import (
    ERROR_RESPONSES,
    CertsEnvelope,
    NetworkEnvelope,
    SystemEnvelope,
)

router = APIRouter()


def _latest(group: str, node: str | None = None) -> dict:
    return get_aggregator().latest_for(group, node)


@router.get("/system", responses={200: {"model": SystemEnvelope,
                                       "description": "系统资源快照"},
                                  **ERROR_RESPONSES})
async def system(node: str | None = Query(None)):
    payload = _latest(constants.GROUP_SYSTEM, node)
    data = {
        "cpu_percent": payload.get("cpu_percent"),
        "mem_percent": payload.get("mem_percent"),
        "disk": payload.get("disk"),
        "load": payload.get("load"),
        "net": payload.get("net"),
        "ts": payload.get("ts"),
    }
    # 快照缺失时无条件回退 samples 最新值（重启后首轮采集前）
    if data["cpu_percent"] is None:
        latest = db.query_latest_metrics(constants.GROUP_SYSTEM,
                                         ["cpu_percent", "mem_percent"],
                                         node_id=node or config.NODE_ID)
        data["cpu_percent"] = latest.get("cpu_percent")
        data["mem_percent"] = latest.get("mem_percent")
    return ok(data)


@router.get("/certs", responses={200: {"model": CertsEnvelope,
                                      "description": "证书到期快照"},
                                 **ERROR_RESPONSES})
async def certs(node: str | None = Query(None)):
    payload = _latest(constants.GROUP_CERT_DAYS, node)
    certs = payload.get("certs", [])
    return ok({"certs": certs, "ts": payload.get("ts")})


@router.get("/network", responses={200: {"model": NetworkEnvelope,
                                        "description": "网络探测快照"},
                                   **ERROR_RESPONSES})
async def network(node: str | None = Query(None)):
    payload = _latest(constants.GROUP_NETWORK, node)
    return ok({"probes": payload.get("probes", []), "ts": payload.get("ts")})
