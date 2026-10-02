"""GRM（github-release-monitor）UpdateProvider（Phase 3）。

复用既有 GrmCollector 的读取与归一化逻辑（单一实现，/api/grm 旧契约不变），
仅转换为统一 UpdateItem 形态供 /api/updates 与 update_items 表消费。
"""
from __future__ import annotations

import json
import logging
import time

from app import config
from app.collectors.grm import GrmCollector
from app.collectors.updates.base import UpdateItem, UpdateProvider, UpdateSourceError

log = logging.getLogger(__name__)


def rows_to_items(node_id: str, rows: list[dict], ts: float) -> list[dict]:
    """grm_repos 行 → update_items 行（聚合层双写用）。"""
    out = []
    for r in rows:
        out.append({
            "provider": "grm",
            "item_key": r.get("repo_id") or r.get("url") or "",
            "title": r.get("display_name") or r.get("owner_slug"),
            "url": r.get("url"),
            "version": r.get("latest_tag"),
            "version_url": r.get("latest_url"),
            "is_new": 1 if r.get("is_new") else 0,
            "prerelease": 1 if r.get("latest_prerelease") else 0,
            "tags": r.get("tags") or "[]",
            "published_ts": r.get("published_ts"),
            "updated_ts": ts,
        })
    return out


class GrmProvider(UpdateProvider):
    name = "grm"

    def __init__(self, mock: bool | None = None) -> None:
        self._collector = GrmCollector(mock)

    async def fetch(self) -> list[UpdateItem]:
        result = await self._collector.run_once()
        payload = result.payload or {}
        if payload.get("degraded"):
            raise UpdateSourceError(payload.get("error") or "grm 数据源不可读")
        rows = payload.get("repos") or []
        ts = time.time()
        items = []
        for row in rows_to_items(config.NODE_ID, rows, ts):
            tags = row.pop("tags", "[]")
            try:
                tag_list = json.loads(tags or "[]")
            except json.JSONDecodeError:
                tag_list = []
            items.append(UpdateItem(
                provider=row["provider"], item_key=row["item_key"],
                title=row["title"], url=row["url"], version=row["version"],
                version_url=row["version_url"],
                is_new=bool(row["is_new"]), prerelease=bool(row["prerelease"]),
                tags=tag_list, published_ts=row["published_ts"]))
        return items


async def fetch_all(names: list[str] | None = None) -> dict[str, list[UpdateItem]]:
    """并发拉取多个 provider（单个失败不拖垮其他，照 grm 降级先例记日志）。"""
    from app.collectors.updates import PROVIDERS
    out: dict[str, list[UpdateItem]] = {}
    for name in names or list(PROVIDERS):
        provider = PROVIDERS.get(name)
        if provider is None:
            log.warning("updates: unknown provider %r skipped", name)
            continue
        try:
            out[name] = await provider.fetch()
        except UpdateSourceError as exc:
            log.warning("updates: provider %s degraded: %s", name, exc)
            out[name] = []
    return out


__all__ = ["GrmProvider", "rows_to_items", "fetch_all"]
