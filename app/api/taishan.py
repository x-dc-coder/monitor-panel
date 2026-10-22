"""GET /api/taishan：TaiShan 出网质量快照（读内存快照，重启回退快照表）。"""
from __future__ import annotations

from fastapi import APIRouter, Query

from app import config, constants, db
from app.api import get_aggregator, ok
from app.api.schemas import ERROR_RESPONSES, TaishanEnvelope

router = APIRouter()


@router.get("/taishan", responses={200: {"model": TaishanEnvelope,
                                        "description": "TaiShan 出网质量快照"},
                                   **ERROR_RESPONSES})
async def taishan(node: str | None = Query(None)):
    payload = get_aggregator().latest_for(constants.GROUP_TAISHAN, node)
    if not payload:
        # 重启后内存快照为空：回退 taishan_snapshot 快照表
        payload = db.get_taishan_snapshot(node or config.NODE_ID) or {}
    data = {
        "active_node": payload.get("active_node"),
        "state": payload.get("state"),
        "summary": payload.get("summary"),
        "qualified_count": payload.get("qualified_count"),
        "top_candidates": payload.get("top_candidates") or [],
        "switched": payload.get("switched"),
        "reason": payload.get("reason"),
        "ts": payload.get("ts"),
    }
    return ok(data)
