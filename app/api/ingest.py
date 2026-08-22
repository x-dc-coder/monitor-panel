"""POST /api/ingest：远端 agent 上报（cloud-2 接入用，issue #5）。

- nginx 对 `location /api/ingest` 已 `auth_basic off`（浏览器 Basic Auth 豁免），
  防护全部落在本接口的应用层 Bearer token
- agent 调用方式：`Authorization: Bearer $MONITOR_INGEST_TOKEN`，
  token 与启用开关均配置于 /opt/monitor-panel/env（见 .env.example ingest 节）
- token 比较用 hmac.compare_digest（常量时间，抗时序侧信道）
- 数据契约与自采同构（CollectorResult 批量），启用后零迁移
- INGEST_ENABLED=0 时返回 404（接口不可发现），启用后无/错 token 返回 401
"""
from __future__ import annotations

import hmac
import logging

from fastapi import APIRouter, Depends, Header, HTTPException

from app import config
from app.api import get_aggregator, ok

log = logging.getLogger(__name__)
router = APIRouter()


def _bearer(authorization: str | None = Header(None)) -> None:
    """应用层 token 校验（nginx Basic Auth 豁免后的唯一防线）。"""
    if not config.INGEST_ENABLED:
        return
    expected = f"Bearer {config.INGEST_TOKEN}"
    if not hmac.compare_digest(authorization or "", expected):
        raise HTTPException(
            status_code=401,
            detail="invalid token",
            headers={"WWW-Authenticate": "Bearer"})


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
