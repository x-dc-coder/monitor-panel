"""UpdateProvider 统一抽象（Phase 3）。

约定：
- fetch() 异步返回 list[UpdateItem]；数据源不可读时抛 UpdateSourceError，
  由调用方决定降级语义（保留上一轮快照，照 grm/frp 先例）
- item_key 在 provider 内唯一（UNIQUE(node_id, provider, item_key)）
- 新 provider 只需实现本接口并注册进 app/collectors/updates/__init__.py
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field


class UpdateSourceError(Exception):
    """数据源不可读（文件缺失/上游异常等），调用方降级处理。"""


@dataclass
class UpdateItem:
    provider: str
    item_key: str
    title: str | None = None
    url: str | None = None
    version: str | None = None           # 最新版本 / tag
    version_url: str | None = None
    is_new: bool = False                 # 新发布标记
    prerelease: bool = False
    tags: list[str] = field(default_factory=list)
    published_ts: float | None = None

    def to_row(self, updated_ts: float) -> dict:
        import json
        return {
            "provider": self.provider,
            "item_key": self.item_key,
            "title": self.title,
            "url": self.url,
            "version": self.version,
            "version_url": self.version_url,
            "is_new": 1 if self.is_new else 0,
            "prerelease": 1 if self.prerelease else 0,
            "tags": json.dumps(self.tags, ensure_ascii=False),
            "published_ts": self.published_ts,
            "updated_ts": updated_ts,
        }


class UpdateProvider(abc.ABC):
    """服务更新数据源接口。"""

    name: str = "base"

    @abc.abstractmethod
    async def fetch(self) -> list[UpdateItem]:
        """拉取本源的更新条目；不可读时抛 UpdateSourceError。"""
        raise NotImplementedError
