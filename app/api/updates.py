"""GET /api/updates：服务更新统一入口（Phase 3：多源聚合）。

- 数据权威源：db.update_items 表（各 UpdateProvider 经聚合层写入；grm 当前
  由 /api/grm 采集链路双写）
- 只读：仓库/版本源配置在各 provider 侧完成，本接口无写操作
- ?provider=grm 过滤单一数据源；providers 字段返回已注册源清单（前端留口）
"""
from __future__ import annotations

import json
import time

from fastapi import APIRouter

from app import db
from app.api import get_aggregator, ok
from app.collectors.updates import PROVIDERS

router = APIRouter()


@router.get("/updates")
async def updates(provider: str | None = None, node: str | None = None):
    snapshot = get_aggregator().latest.get("grm") or {}
    rows = db.get_update_items(node_id=node, provider=provider)
    out = []
    for r in rows:
        try:
            tags = json.loads(r.get("tags") or "[]")
        except json.JSONDecodeError:
            tags = []
        out.append({
            "provider": r.get("provider"),
            "item_key": r.get("item_key"),
            "title": r.get("title"),
            "url": r.get("url"),
            "version": r.get("version"),
            "version_url": r.get("version_url"),
            "is_new": bool(r.get("is_new")),
            "prerelease": bool(r.get("prerelease")),
            "tags": tags,
            "published_ts": r.get("published_ts"),
            "updated_ts": r.get("updated_ts"),
        })
    return ok({
        "items": out,
        "providers": sorted(PROVIDERS),
        "summary": {
            "total": len(out),
            "new": sum(1 for r in out if r["is_new"]),
            "with_version": sum(1 for r in out if r["version"]),
            "degraded": bool(snapshot.get("degraded")),
            "error": snapshot.get("error"),
        },
        "ts": time.time(),
    })
