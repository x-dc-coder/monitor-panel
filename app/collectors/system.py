"""云服务器健康采集器（F3，psutil）。

- CPU/内存/负载/网络吞吐；磁盘分区百分比（扫描走 asyncio.to_thread，评审 B）
- 网络吞吐：/proc/net/dev 计数器差值 → kb/s（首轮为 0）
"""
from __future__ import annotations

import asyncio
import logging
import time

from app import config, constants
from app.collectors.base import Collector, CollectorResult

log = logging.getLogger(__name__)


class SystemCollector(Collector):
    name = "system"
    group = constants.GROUP_SYSTEM
    interval = 30

    def __init__(self, mock: bool | None = None) -> None:
        super().__init__(mock)
        self._last_net: dict[str, int] | None = None
        self._last_net_ts: float = 0.0

    # ---------------- mock ----------------
    def _mock_result(self) -> CollectorResult:
        ts = time.time()
        result = CollectorResult(node_id=config.NODE_ID, group=self.group)
        vals = {"cpu_percent": 12.3, "mem_percent": 41.2, "disk_percent": 53.0,
                "load_1m": 0.42, "load_5m": 0.38, "load_15m": 0.35,
                "net_rx_kbps": 128.4, "net_tx_kbps": 64.2}
        for k, v in vals.items():
            result.add(k, v, ts)
        result.payload = {
            "cpu_percent": vals["cpu_percent"], "mem_percent": vals["mem_percent"],
            "disk": {"/": {"percent": 53.0, "total_gb": 100.0, "used_gb": 53.0},
                     "/data": {"percent": 41.0, "total_gb": 200.0, "used_gb": 82.0}},
            "load": [vals["load_1m"], vals["load_5m"], vals["load_15m"]],
            "net": {"rx_kbps": vals["net_rx_kbps"], "tx_kbps": vals["net_tx_kbps"]},
            "ts": ts, "mock": True,
        }
        return result

    # ---------------- real ----------------
    @staticmethod
    def _read_net_counters() -> dict[str, int]:
        """/proc/net/dev 主网卡累计字节。"""
        counters: dict[str, int] = {}
        try:
            with open("/proc/net/dev", "r", encoding="utf-8") as f:
                for line in f.readlines()[2:]:
                    if ":" not in line:
                        continue
                    iface, rest = line.split(":", 1)
                    parts = rest.split()
                    if len(parts) >= 9 and iface.strip() != "lo":
                        counters[f"{iface.strip()}:rx"] = int(parts[0])
                        counters[f"{iface.strip()}:tx"] = int(parts[8])
        except OSError:
            pass
        return counters

    @staticmethod
    def _disk_scan() -> tuple[dict, list]:
        """磁盘分区（to_thread）。返回 (disk 摘要, 指标列表)。"""
        import shutil
        import os
        disk: dict = {}
        metrics: list[tuple[str, float]] = []
        try:
            for part in __import__("psutil").disk_partitions(all=False):
                if part.fstype in ("squashfs", "tmpfs", "devtmpfs", "overlay"):
                    continue
                try:
                    usage = shutil.disk_usage(part.mountpoint)
                    pct = round(usage.used / usage.total * 100, 1) if usage.total else 0.0
                except OSError:
                    continue
                disk[part.mountpoint] = {"percent": pct,
                                         "total_gb": round(usage.total / 2**30, 1),
                                         "used_gb": round(usage.used / 2**30, 1)}
                if part.mountpoint == "/":
                    metrics.append(("disk_percent", pct))
                else:
                    safe = part.mountpoint.replace("/", "_").lstrip("_") or "root"
                    metrics.append((f"disk_percent.{safe}", pct))
        except Exception:  # noqa: BLE001
            log.warning("system: disk scan failed", exc_info=True)
        return disk, metrics

    @staticmethod
    def _psutil_snapshot() -> dict:
        import psutil
        import os
        cpu = psutil.cpu_percent(interval=None) or 0.0
        mem = psutil.virtual_memory().percent
        try:
            load = list(os.getloadavg())
        except OSError:
            load = [0.0, 0.0, 0.0]
        return {"cpu_percent": round(cpu, 1), "mem_percent": round(mem, 1),
                "load": [round(x, 2) for x in load]}

    async def _run_real(self) -> CollectorResult:
        ts = time.time()
        result = CollectorResult(node_id=config.NODE_ID, group=self.group)
        # 同步阻塞调用（psutil 快照/磁盘扫描）走 asyncio.to_thread（评审 B）
        snap = await asyncio.to_thread(self._psutil_snapshot)
        disk, disk_metrics = await asyncio.to_thread(self._disk_scan)

        metrics: list[tuple[str, float]] = [
            ("cpu_percent", snap["cpu_percent"]),
            ("mem_percent", snap["mem_percent"]),
            ("load_1m", snap["load"][0]),
            ("load_5m", snap["load"][1]),
            ("load_15m", snap["load"][2]),
        ] + disk_metrics

        # 网络吞吐（计数器差值）
        net_now = self._read_net_counters()
        rx_kbps = tx_kbps = 0.0
        if self._last_net and self._last_net_ts:
            dt = ts - self._last_net_ts
            if dt > 0:
                rx = sum(net_now.get(k, 0) for k in net_now if k.endswith(":rx")) -                     sum(self._last_net.get(k, 0) for k in self._last_net if k.endswith(":rx"))
                tx = sum(net_now.get(k, 0) for k in net_now if k.endswith(":tx")) -                     sum(self._last_net.get(k, 0) for k in self._last_net if k.endswith(":tx"))
                rx_kbps = round(max(0.0, rx) * 8 / 1024 / dt, 1)
                tx_kbps = round(max(0.0, tx) * 8 / 1024 / dt, 1)
        self._last_net = net_now
        self._last_net_ts = ts
        metrics += [("net_rx_kbps", rx_kbps), ("net_tx_kbps", tx_kbps)]

        for name, value in metrics:
            result.add(name, value, ts)
        result.payload = {
            "cpu_percent": snap["cpu_percent"], "mem_percent": snap["mem_percent"],
            "disk": disk, "load": snap["load"],
            "net": {"rx_kbps": rx_kbps, "tx_kbps": tx_kbps}, "ts": ts,
        }
        return result

    async def _mock_run(self) -> CollectorResult:
        return self._mock_result()
