"""GET /api/updates：服务更新统一入口（Phase 3：多源聚合）。

- 数据权威源：db.update_items 表（各 UpdateProvider 经聚合层写入；grm 当前
  由 /api/grm 采集链路双写）
- 只读：仓库/版本源配置在各 provider 侧完成，本接口无写操作
- ?provider=grm 过滤单一数据源；providers 字段返回已注册源清单（前端留口）
"""
from __future__ import annotations

import json

from fastapi import APIRouter

from app import db
from app.api import get_aggregator, ok
from app.api.schemas import ERROR_RESPONSES, UpdatesEnvelope
from app.collectors.updates import PROVIDERS

router = APIRouter()


@router.get("/updates", responses={200: {"model": UpdatesEnvelope,
                                        "description": "服务更新多源聚合"},
                                   **ERROR_RESPONSES})
async def updates(provider: str | None = None, node: str | None = None):
    snapshot = get_aggregator().latest_for("grm", node)
    rows = db.get_update_items(node_id=node, provider=provider)
    # 读侧合并 provider 富字段（P1-4）：update_items 保持泛型，is_pinned/summary
    # 取自 grm_repos；item_key≡repo_id。非 grm 过滤时无需查询。
    grm_by_id: dict[str, dict] = {}
    if provider is None or provider == "grm":
        grm_by_id = {r["repo_id"]: r for r in db.get_grm_repos(node_id=node)
                     if r.get("repo_id")}
    out = []
    for r in rows:
        try:
            tags = json.loads(r.get("tags") or "[]")
        except json.JSONDecodeError:
            tags = []
        grm_row = grm_by_id.get(r.get("item_key") or "")
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
            "is_pinned": bool(grm_row.get("is_pinned")) if grm_row else False,
            "summary": grm_row.get("readme_summary") if grm_row else None,
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
    })
