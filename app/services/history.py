"""趋势查询：range + bucket 降采样（AVG）。"""
from __future__ import annotations

import time

from app import db

RANGES = {
    "1h": 3600,
    "24h": 86400,
    "7d": 604800,
    "30d": 2592000,
}
# 目标点数上限：超出时自动放大桶（查询时降采样）
MAX_POINTS = 240


def resolve_range(range_: str) -> int:
    if range_ not in RANGES:
        raise ValueError(f"invalid range {range_!r}, use {sorted(RANGES)}")
    return RANGES[range_]


def query(metric: str, range_: str = "24h", bucket: int | None = None,
          group: str | None = None,
          node_id: str | None = None) -> dict:
    """降采样查询。bucket 未给时按 MAX_POINTS 自动计算。"""
    span = resolve_range(range_)
    since = time.time() - span
    if not bucket or bucket <= 0:
        bucket = max(1, int(span / MAX_POINTS))

    if node_id == "all":
        query_node = None
    elif node_id is None:
        from app import config
        query_node = config.NODE_ID
    else:
        query_node = node_id

    points = db.query_history(metric, since, float(bucket), group=group, node_id=query_node)
    return {
        "metric": metric,
        "group": group,
        "node": node_id,
        "range": range_,
        "bucket_s": bucket,
        "points": points,
        "count": len(points),
        "since_ts": since,
    }
