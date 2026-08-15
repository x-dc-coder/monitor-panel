"""GET /api/system /api/certs /api/network：资源/证书/探测（读快照）。"""
from __future__ import annotations

from fastapi import APIRouter

from app import constants, db
from app.api import get_aggregator, ok

router = APIRouter()


def _latest(group: str) -> dict:
    return get_aggregator().latest.get(group, {})


@router.get("/system")
async def system():
    agg = get_aggregator()
    payload = _latest(constants.GROUP_SYSTEM)
    data = {
        "cpu_percent": payload.get("cpu_percent"),
        "mem_percent": payload.get("mem_percent"),
        "disk": payload.get("disk"),
        "load": payload.get("load"),
        "net": payload.get("net"),
        "ts": payload.get("ts"),
    }
    # 快照缺失时回退 samples 最新值（重启后首轮采集前）
    if data["cpu_percent"] is None:
        latest = agg.latest_ts and agg.latest_ts.get(constants.GROUP_SYSTEM)
        if latest is None:
            latest = db.query_latest_by_group(constants.GROUP_SYSTEM)
            data["cpu_percent"] = latest.get("cpu_percent")
            data["mem_percent"] = latest.get("mem_percent")
    return ok(data)


@router.get("/certs")
async def certs():
    payload = _latest(constants.GROUP_CERT_DAYS)
    certs = payload.get("certs", [])
    return ok({"certs": certs, "ts": payload.get("ts")})


@router.get("/network")
async def network():
    payload = _latest(constants.GROUP_NETWORK)
    return ok({"probes": payload.get("probes", []), "ts": payload.get("ts")})
