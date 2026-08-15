"""POST /api/ingest：远端 agent 上报预留（二期经 nginx 暴露，本期默认 404）。

- 服务仅监听 127.0.0.1，远端节点无法直连 → 二期 nginx 单独 location +
  Basic Auth + Bearer 双鉴权（评审 E10）；本期默认禁用
- 数据契约与自采同构（CollectorResult 批量），启用后零迁移
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Header, HTTPException

from app import config
from app.api import get_aggregator, ok

log = logging.getLogger(__name__)
router = APIRouter()


def _bearer(authorization: str | None = Header(None)) -> None:
    if not config.INGEST_ENABLED:
        return
    expected = f"Bearer {config.INGEST_TOKEN}"
    if authorization != expected:
        raise HTTPException(status_code=401, detail="invalid token")


@router.post("/ingest", dependencies=[Depends(_bearer)],
             include_in_schema=False)
async def ingest(body: dict):
    if not config.INGEST_ENABLED:
        raise HTTPException(status_code=404,
                            detail="ingest not enabled (planned for M5 / cloud-2)")
    # 二期：校验 Bearer + 与自采同构的 CollectorResult 批量，交由聚合层入库
    agg = get_aggregator()
    results = body.get("results") if isinstance(body, dict) else None
    if not isinstance(results, list):
        raise HTTPException(status_code=400, detail="expected {results: [...]}")
    n = 0
    for item in results:
        if not isinstance(item, dict) or "node_id" not in item or "group" not in item:
            continue
        from app.collectors.base import CollectorResult, Metric
        r = CollectorResult(node_id=item["node_id"], group=item["group"])
        for m in item.get("metrics", []):
            r.add(m["name"], m["value"], m.get("ts"))
        if item.get("payload"):
            r.payload = item["payload"]
        await agg.ingest(r)
        n += 1
    log.info("ingest: %d result(s) from remote node", n)
    return ok({"accepted": n})
