"""GET /api/grm：Release 监控（issue #4：只读聚合 github-release-monitor）。

- 数据权威源：db.grm_repos 表（grm 采集器每轮 upsert）
- 仓库增删等配置在 releases.monitor.dc-sy.cn 完成，本接口不含任何写操作
- grm 不可读时：summary.degraded=true + error（采集器降级先例），其余区块不受影响
"""
from __future__ import annotations

import json

from fastapi import APIRouter

from app import config, db
from app.api import get_aggregator, ok
from app.api.schemas import GrmEnvelope

router = APIRouter()


@router.get("/grm", responses={200: {"model": GrmEnvelope,
                                     "description": "Release 监控（grm 只读聚合）"}})
async def grm(node: str | None = None):
    agg = get_aggregator()
    snapshot = agg.latest_for("grm", node)
    rows = db.get_grm_repos(node or config.NODE_ID)
    out = []
    for r in rows:
        out.append({
            "repo_id": r.get("repo_id"),
            "url": r.get("url"),
            "owner_slug": r.get("owner_slug"),
            "display_name": r.get("display_name"),
            "tags": json.loads(r.get("tags") or "[]"),
            "is_pinned": bool(r.get("is_pinned")),
            "is_new": bool(r.get("is_new")),
            "latest": {
                "tag": r.get("latest_tag"),
                "name": r.get("latest_name"),
                "url": r.get("latest_url"),
                "prerelease": bool(r.get("latest_prerelease")),
                "published_ts": r.get("published_ts"),
            } if r.get("latest_tag") else None,
            # grm fork 扩展字段（readmeSummary/recentCommits 就绪后自动展示）
            "readme_summary": r.get("readme_summary"),
            "recent_commits": json.loads(r.get("recent_commits") or "[]"),
            "first_seen_ts": r.get("first_seen_ts"),
            "updated_ts": r.get("updated_ts"),
        })
    return ok({
        "repos": out,
        "summary": {
            "total": len(out),
            "with_release": sum(1 for r in out if r["latest"]),
            "new_release": sum(1 for r in out if r["is_new"]),
            "degraded": bool(snapshot.get("degraded")),
            "error": snapshot.get("error"),
        },
        "base_url": config.GRM_BASE_URL,
    })
