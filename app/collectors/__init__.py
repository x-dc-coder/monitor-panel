"""采集器注册表（周期 → collector 实例）。

- build_registry() 按 config.INTERVALS 覆盖各采集器周期
- 模块级 registry 在 main lifespan 中构建，供 API（手动 ping）复用采集器实例
"""
from __future__ import annotations

import logging

from app import config
from app.collectors.base import Collector, CollectorResult
from app.collectors.certs import CertsCollector
from app.collectors.frp import FrpCollector
from app.collectors.headscale import HeadscaleCollector
from app.collectors.network import NetworkCollector
from app.collectors.services import ServicesCollector
from app.collectors.system import SystemCollector
from app.collectors.tailscale import TailscaleCollector

log = logging.getLogger(__name__)

COLLECTOR_CLASSES = (
    FrpCollector,
    TailscaleCollector,
    HeadscaleCollector,
    SystemCollector,
    NetworkCollector,
    CertsCollector,
    ServicesCollector,
)

registry: dict[str, Collector] = {}


def build_registry() -> dict[str, Collector]:
    """构建注册表：mock 开关取自 config.MOCK；周期优先取 config.INTERVALS 同名键。"""
    for cls in COLLECTOR_CLASSES:
        c = cls()
        c.interval = config.INTERVALS.get(c.name, c.interval)
        registry[c.name] = c
    log.info("collectors: %s (mock=%s)", ", ".join(registry), config.MOCK)
    return registry


def get(name: str) -> Collector | None:
    return registry.get(name)


__all__ = ["Collector", "CollectorResult", "build_registry", "registry", "get",
           "FrpCollector", "TailscaleCollector", "HeadscaleCollector",
           "SystemCollector", "NetworkCollector", "CertsCollector",
           "ServicesCollector"]
