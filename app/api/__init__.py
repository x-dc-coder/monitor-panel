"""只读 API（除预留 /api/ingest 外无写端点）。

统一响应契约：{ok, data, ts}。
共享状态（aggregator / registry）由 main lifespan 注入。
"""
from __future__ import annotations

import time

from app.services.aggregator import Aggregator

aggregator: Aggregator | None = None


def set_aggregator(agg: Aggregator) -> None:
    global aggregator
    aggregator = agg


def get_aggregator() -> Aggregator:
    if aggregator is None:
        raise RuntimeError("aggregator not initialized")
    return aggregator


def ok(data) -> dict:
    return {"ok": True, "data": data, "ts": time.time()}


def err(message: str, code: int = 400) -> tuple[dict, int]:
    return {"ok": False, "error": message, "ts": time.time()}, code
