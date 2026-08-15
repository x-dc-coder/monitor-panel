"""GET /api/history?metric=&range=&bucket=&group=：趋势降采样（F8）。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from app import constants
from app.api import ok
from app.services import history as history_svc

router = APIRouter()


@router.get("/history")
async def history(metric: str = Query(...),
                  range_: str = Query("24h", alias="range"),
                  bucket: int | None = Query(None),
                  group: str | None = Query(None)):
    # metric 白名单校验（评审 A 遗漏 2）
    if group:
        if not constants.is_valid_metric(group, metric):
            raise HTTPException(
                status_code=400,
                detail=f"invalid metric {metric!r} for group {group!r}")
    elif not any(constants.is_valid_metric(g, metric)
                 for g in constants.GROUP_METRICS):
        raise HTTPException(status_code=400, detail=f"unknown metric {metric!r}")
    try:
        data = history_svc.query(metric, range_, bucket, group=group)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ok(data)
