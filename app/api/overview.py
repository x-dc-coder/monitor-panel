"""GET /api/overview：聚合总览（前端主轮询点）。"""
from __future__ import annotations

from fastapi import APIRouter

from app.api import get_aggregator, ok
from app.api.schemas import AlertsEnvelope, OverviewEnvelope

router = APIRouter()


@router.get("/overview", responses={200: {"model": OverviewEnvelope,
                                         "description": "聚合总览"}})
async def overview(node: str | None = None):
    data = get_aggregator().overview(node_id=node)
    return ok(data)


@router.get("/alerts", responses={200: {"model": AlertsEnvelope,
                                       "description": "告警事件流"}})
async def alerts(limit: int = 50, unresolved_only: bool = False):
    """告警事件流（F9 MVP：面板内红黄灯 + 事件流；webhook 二期）。"""
    from app import db
    return ok({"alerts": db.get_alerts(limit=limit, unresolved_only=unresolved_only)})
