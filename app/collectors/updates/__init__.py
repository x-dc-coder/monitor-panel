"""服务更新多源聚合（Phase 3：「留口子」设计）。

统一抽象：UpdateItem（规范化更新条目）+ UpdateProvider（数据源接口）。
首个实现 GrmProvider 封装既有 github-release-monitor 只读聚合；
后续新数据源（容器镜像 tag、AUR/pypi 等）实现同一接口即可进入
/api/updates 与 update_items 表，无需改 API 层。
"""
from app.collectors.updates.base import UpdateItem, UpdateProvider
from app.collectors.updates.grm import GrmProvider

PROVIDERS: dict[str, UpdateProvider] = {
    "grm": GrmProvider(),
}


def get_provider(name: str) -> UpdateProvider | None:
    return PROVIDERS.get(name)


__all__ = ["UpdateItem", "UpdateProvider", "GrmProvider", "PROVIDERS",
           "get_provider"]
