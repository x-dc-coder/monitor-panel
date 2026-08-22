"""Release 监控采集器（issue #4：只读聚合 github-release-monitor）。

- 数据源：grm 数据目录下的 repositories.json（与 grm 同机部署，零 API 依赖、
  无认证问题）；grm 侧负责仓库增删/通知渠道等全部配置，本采集器只读
- 降级语义（照 frp 先例）：数据文件缺失/损坏 → payload 带 degraded=true +
  error，仍正常返回，不影响其他区块；前端显示"Release 监控暂不可用"
- 无 release 仓库：latestRelease 缺失时降级展示（README 摘要/最近提交字段
  由 grm fork 扩展提供，字段就绪前显示占位）
"""
from __future__ import annotations

import json
import logging
import time

from app import config, constants
from app.collectors.base import Collector, CollectorResult

log = logging.getLogger(__name__)

# mock 确定性样例（字段对齐 grm src/types/index.ts Repository）
_MOCK_REPOS = [
    {"id": "github:x-dc-coder/monitor-panel",
     "url": "https://github.com/x-dc-coder/monitor-panel",
     "displayName": "monitor-panel", "isPinned": True, "tags": ["ops", "panel"],
     "isNew": False,
     "latestRelease": {"html_url": "https://github.com/x-dc-coder/monitor-panel/releases/tag/v1.2",
                       "tag_name": "v1.2", "name": "v1.2 M4/M5",
                       "published_at": "2026-08-17T00:00:00Z",
                       "prerelease": False}},
    {"id": "github:x-dc-coder/github-release-monitor",
     "url": "https://github.com/x-dc-coder/github-release-monitor",
     "displayName": None, "isPinned": False, "tags": ["monitor"],
     "isNew": True,
     "latestRelease": {"html_url": "https://github.com/x-dc-coder/github-release-monitor/releases/tag/v2.1",
                       "tag_name": "v2.1", "name": None,
                       "published_at": "2026-08-20T00:00:00Z",
                       "prerelease": True}},
    {"id": "github:example/no-release-repo",
     "url": "https://github.com/example/no-release-repo",
     "displayName": "无 release 示例", "isPinned": False, "tags": [],
     "isNew": False, "latestRelease": None},
]


def _owner_slug(repo: dict) -> str:
    """owner/repo 显示名：取 id 最后一个冒号之后的最后两段（兼容
    <provider>:owner/repo 与 <provider>:<instance>:owner/repo），
    回退 url（.../owner/repo）解析；均失败时返回 id/url 原文。"""
    rid = repo.get("id") or ""
    core = rid.rpartition(":")[2] if ":" in rid else rid
    if "/" in core:
        tail = core.split("/")[-2:]
        if len(tail) == 2 and all(tail):
            return "/".join(tail)
    url = repo.get("url") or ""
    parts = url.rstrip("/").rsplit("/", 2)
    if len(parts) == 3 and parts[1] and parts[2]:
        return f"{parts[1]}/{parts[2]}"
    return rid or url


def _normalize(repo: dict, ts: float) -> dict:
    """grm Repository → 本面板 grm_repos 行（fork 扩展字段缺失时留 None，前端占位）。"""
    latest = repo.get("latestRelease") or {}
    published = latest.get("published_at") or latest.get("created_at")
    return {
        "repo_id": repo.get("id") or repo.get("url") or "",
        "url": repo.get("url"),
        "owner_slug": _owner_slug(repo),
        "display_name": repo.get("displayName"),
        "tags": json.dumps(repo.get("tags") or [], ensure_ascii=False),
        "is_pinned": 1 if repo.get("isPinned") else 0,
        "is_new": 1 if repo.get("isNew") else 0,
        "latest_tag": latest.get("tag_name"),
        "latest_name": latest.get("name"),
        "latest_url": latest.get("html_url"),
        "latest_prerelease": 1 if latest.get("prerelease") else 0,
        "published_ts": _iso_to_ts(published),
        # grm fork 扩展字段（A/B/C 系列 issue 落地后自动展示，当前恒 None）
        "readme_summary": repo.get("readmeSummary"),
        "recent_commits": json.dumps(repo.get("recentCommits") or [],
                                     ensure_ascii=False),
        "updated_ts": ts,
    }


def _iso_to_ts(value: str | None) -> float | None:
    """ISO8601（GitHub/Zulu 格式）→ epoch 秒；失败返回 None。"""
    if not value:
        return None
    from datetime import datetime
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


class GrmCollector(Collector):
    name = "grm"
    group = constants.GROUP_GRM
    interval = 300

    def _payload(self, repos: list[dict], ts: float, mock: bool) -> CollectorResult:
        result = CollectorResult(node_id=config.NODE_ID, group=self.group)
        rows = [_normalize(r, ts) for r in repos if isinstance(r, dict)]
        result.payload = {
            "repos": rows,
            "summary": {
                "total": len(rows),
                "with_release": sum(1 for r in rows if r["latest_tag"]),
                "new_release": sum(1 for r in rows if r["is_new"]),
            },
            "base_url": config.GRM_BASE_URL,
            "ts": ts, "mock": mock,
        }
        result.add("repos_total", float(len(rows)), ts)
        result.add("repos_with_release",
                   float(sum(1 for r in rows if r["latest_tag"])), ts)
        result.add("repos_new", float(sum(1 for r in rows if r["is_new"])), ts)
        return result

    def _degraded(self, error: str, ts: float) -> CollectorResult:
        """数据源不可读：降级 payload（frp 先例），不影响其他区块。"""
        result = CollectorResult(node_id=config.NODE_ID, group=self.group)
        result.payload = {"repos": [], "summary": {"total": 0, "with_release": 0,
                                                   "new_release": 0},
                          "base_url": config.GRM_BASE_URL,
                          "degraded": True, "error": error[:200],
                          "ts": ts, "mock": False}
        return result

    async def _mock_run(self) -> CollectorResult:
        return self._payload(_MOCK_REPOS, time.time(), mock=True)

    async def _run_real(self) -> CollectorResult:
        ts = time.time()
        path = config.GRM_DATA_PATH
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            log.warning("grm: data file missing (%s) — degraded", path)
            return self._degraded(f"repositories.json 不存在：{path}", ts)
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("grm: data file unreadable (%s): %s — degraded", path, exc)
            return self._degraded(f"repositories.json 读取失败：{exc}", ts)
        if not isinstance(data, list):
            log.warning("grm: unexpected payload shape (%s)", type(data).__name__)
            return self._degraded("repositories.json 结构异常（期望数组）", ts)
        return self._payload(data, ts, mock=False)
