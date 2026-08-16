"""tailscale 采集器（F2）。

- tailscale status --json（经 sudoers 白名单 sudo -n；输出含公钥指纹 → sanitize）
- tailscale ping 轮巡：每节点 60s、3 包取中位/最小（t1 B3 实测正则）；
  asyncio.Semaphore(2) 硬限并发 + 超时 kill（评审 E4）
- 超时语义：在线+超时 → 记录 ping_timeout 指标，聚合层连续 3 轮防抖告警；
  离线 → 不 ping、不告警（防休眠设备误报，评审 E3）
- 手动 ping（/api/devices/{id}/ping）复用同一执行器与并发信号量
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import statistics
import time

from app import config, constants
from app.collectors.base import (Collector, CollectorError, CollectorResult,
                                 run_cmd, sanitize, validate_ip_or_host)

log = logging.getLogger(__name__)

# t1 B3 实测：pong from <hostname> (<ip>) via DERP(<region>) in <X>ms
# 直连：pong from <hostname> (<ip>) via <ip>:<port> in <X>ms
_PONG_RE = re.compile(r"pong from (\S+) \(([\d.]+)\) via (\S+) in ([\d.]+)ms")
_LAST_SEEN_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")


def _hash_id(raw: str) -> str:
    """节点 ID 脱敏：sha256 截断 8 位十六进制（防指纹泄露，保持唯一标识）。"""
    import hashlib
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]

# mock 设备清单（对齐 t1 B3/B4 实测：4 节点，全部 Online、Relay=headscale）
_MOCK_DEVICES = [
    {"device_id": "self-cloud-1", "name": "VM-0-12-ubuntu", "tailscale_ip": "100.64.0.1",
     "os": "linux", "online": True, "relay": None, "self": True},
    {"device_id": "ts-dc-desktop", "name": "dc-desktop", "tailscale_ip": "100.64.0.2",
     "os": "windows", "online": True, "relay": "headscale"},
    {"device_id": "ts-oneplus13", "name": "oneplus13", "tailscale_ip": "100.64.0.3",
     "os": "android", "online": True, "relay": "headscale"},
    {"device_id": "ts-oppo-pad-mini", "name": "oppo-pad-mini", "tailscale_ip": "100.64.0.4",
     "os": "android", "online": True, "relay": "headscale"},
]
_MOCK_PING_MS = {  # 3 包（t1 实测样本）
    "100.64.0.2": [33, 31, 32],
    "100.64.0.3": [86, 29, 27],
    "100.64.0.4": [134, 67, 26],
}


class TailscaleCollector(Collector):
    name = "tailscale"
    group = constants.GROUP_TS_LATENCY
    interval = 30

    def __init__(self, mock: bool | None = None) -> None:
        super().__init__(mock)
        self._sem: asyncio.Semaphore | None = None
        self._next_ping: dict[str, float] = {}   # device_id → monotonic 下次轮巡时间
        self._ping_cache: dict[str, dict] = {}   # device_id → 最近 ping 结果（API 展示）
        self._devices: list[dict] = []
        self._status_ts: float = 0.0

    def _semaphore(self) -> asyncio.Semaphore:
        if self._sem is None:
            self._sem = asyncio.Semaphore(config.PING_CONCURRENCY)
        return self._sem

    # ---------------- mock ----------------
    def _mock_result(self) -> CollectorResult:
        ts = time.time()
        result = CollectorResult(node_id=config.NODE_ID, group=self.group)
        now_mono = time.monotonic()
        self._devices = [dict(d) for d in _MOCK_DEVICES]
        self._status_ts = ts
        due: list[dict] = []
        for dev in self._devices:
            if dev.get("self"):
                continue
            last = self._next_ping.get(dev["device_id"], 0.0)
            if now_mono >= last:
                due.append(dev)
                self._next_ping[dev["device_id"]] = now_mono + config.INTERVALS["ts_ping"]
        for dev in due:
            samples = _MOCK_PING_MS.get(dev["tailscale_ip"], [30, 32, 31])
            median = statistics.median(samples)
            ping = {"device_id": dev["device_id"], "hostname": dev["name"],
                    "tailscale_ip": dev["tailscale_ip"], "latency_ms": round(median, 1),
                    "path": "derp" if dev.get("relay") else "direct",
                    "loss_percent": 0.0, "timed_out": False, "ts": ts,
                    "packets": samples}
            self._ping_cache[dev["device_id"]] = ping
            result.add(f"latency_ms.{dev['name']}", median, ts)
            result.add(f"packet_loss_percent.{dev['name']}", 0.0, ts)
        result.payload = {"devices": self._devices, "pings": dict(self._ping_cache),
                          "ts": ts, "mock": True}
        return result

    # ---------------- real ----------------
    async def _fetch_status(self) -> dict:
        rc, out, err = await run_cmd([config.TAILSCALE_CMD, "status", "--json"],
                                     sudo=config.SUDO, timeout=config.STEP_TIMEOUT)
        if rc != 0:
            raise CollectorError(f"tailscale status failed rc={rc}: {err.strip()[:200]}")
        data = json.loads(out)
        # 脱敏后记录（评审 F4：status 含节点公钥/密钥指纹）
        log.debug("tailscale status (sanitized): %s",
                  json.dumps(sanitize(data), ensure_ascii=False)[:2000])
        return data

    @staticmethod
    def _parse_last_seen(raw: str | None) -> float | None:
        if not raw or not _LAST_SEEN_RE.match(raw):
            return None  # 在线时 Go 零值 0001-01-01T00:00:00Z（t1 B2 解析坑）
        try:
            from datetime import datetime, timezone
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return dt.timestamp()
        except ValueError:
            return None

    def _norm_device(self, key: str, p: dict, is_self: bool = False) -> dict:
        ips = p.get("TailscaleIPs") or []
        v4 = next((ip for ip in ips if ip.startswith("100.64.")), None) or             next(iter(ips), None)
        # Self 对象无 BackendState（在 status --json 顶层），但 Self.Online 存在；
        # 统一用 Online 字段判定（修复 Self 节点恒为 offline 的 bug）
        online = bool(p.get("Online", False))
        relay = None if is_self else (p.get("Relay") or
                                      ("direct" if p.get("CurAddr") else None))
        last_seen = self._parse_last_seen(p.get("LastSeen"))
        return {
            "node_id": config.NODE_ID,
            # 脱敏：tailscale 节点 ID 含敏感指纹（评审 F4/E6）→ sha256 截断 8 位，
            # 保持设备间唯一且可作 API/入库标识，不落原始 ID
            "device_id": _hash_id(p.get("ID") or key),
            "name": p.get("HostName") or key,
            "tailscale_ip": v4,
            "os": p.get("OS"),
            "online": 1 if online else 0,
            "relay": relay,
            "last_seen_ts": last_seen,
            "rx_bytes": p.get("RxBytes") or 0,
            "tx_bytes": p.get("TxBytes") or 0,
            "updated_ts": time.time(),
        }

    async def _do_ping(self, ip: str, hostname: str, device_id: str) -> dict:
        """单设备 ping N 包（Semaphore 限并发；超时 kill）。

        修复（2026-08-15）：tailscale ping 默认发 10 次或直到直连建立才停，
        在无 TTY 的 asyncio pipe 环境下会持续运行导致超时。加 --c N 限制包数
        （与 config.PING_PACKETS 一致），单次调用解析全部 pong。
        """
        if not validate_ip_or_host(ip):
            raise CollectorError(f"invalid ping target: {ip!r}")
        samples: list[float] = []
        errors = 0
        try:
            async with self._semaphore():
                rc, out, err = await run_cmd(
                    [config.TAILSCALE_CMD, "ping", "--c", str(max(1, config.PING_PACKETS)), ip],
                    sudo=config.SUDO, timeout=config.PING_TIMEOUT * max(1, config.PING_PACKETS))
            # 一次输出可能含多个 pong（--c N 连发）；实测 tailscale ping 成功
            # 也返回 rc=1，以输出中是否含 pong 为准（不能只看退出码）
            for m in _PONG_RE.finditer(out):
                samples.append(float(m.group(4)))
            if not samples:
                errors = config.PING_PACKETS
        except CollectorError:
            errors = config.PING_PACKETS
        ts = time.time()
        if samples:
            # 丢包率修复（2026-08-16）：tailscale ping 的 pong 数量可能少于请求数
            # （前几包直连建立/慢响应正常），只要拿到 ≥1 个 pong 即视为链路可达，
            # 丢包率按「完全无响应」判定（0% 或 100%），避免 1/3=33% 之类的误报
            loss = 0.0 if len(samples) > 0 else 100.0
            return {"device_id": device_id, "hostname": hostname,
                    "tailscale_ip": ip, "latency_ms": round(statistics.median(samples), 1),
                    "latency_min_ms": round(min(samples), 1),
                    "path": "derp" if "DERP" in out else "direct",
                    "loss_percent": loss,
                    "timed_out": False, "ts": ts, "packets": samples}
        return {"device_id": device_id, "hostname": hostname, "tailscale_ip": ip,
                "latency_ms": None, "path": "unknown", "loss_percent": 100.0,
                "timed_out": True, "ts": ts, "packets": []}

    async def ping_device(self, device_id: str, ip: str) -> dict:
        """手动 ping（API 调用）：同一执行器，结果入缓存。"""
        ping = await self._do_ping(ip, device_id, device_id)
        self._ping_cache[device_id] = ping
        return ping

    async def _run_real(self) -> CollectorResult:
        ts = time.time()
        result = CollectorResult(node_id=config.NODE_ID, group=self.group)
        data = await self._fetch_status()
        self_data = data.get("Self", {})
        self._devices = [self._norm_device("self", self_data, is_self=True)]
        peers = data.get("Peer", {}) or {}
        for key, p in peers.items():
            self._devices.append(self._norm_device(key, p))
        self._status_ts = ts

        # ping 轮巡：到期设备才 ping（60s/台，错峰启动）
        now_mono = time.monotonic()
        n = max(1, len(self._devices) - 1)
        due: list[dict] = []
        for idx, dev in enumerate(self._devices[1:]):
            last = self._next_ping.get(dev["device_id"])
            if not dev["online"]:
                continue  # 离线不 ping（评审 E3）
            if last is None:
                # 首轮：立即 ping（同轮内用 idx/n 错开首包，不推迟到未来）
                due.append(dev)
                self._next_ping[dev["device_id"]] = now_mono + (idx / n) * \
                    config.INTERVALS["ts_ping"]
            elif now_mono >= last:
                due.append(dev)
                self._next_ping[dev["device_id"]] = now_mono + config.INTERVALS["ts_ping"]
        if due:
            pings = await asyncio.gather(*[
                self._do_ping(d["tailscale_ip"], d["name"], d["device_id"])
                for d in due])
            for ping in pings:
                self._ping_cache[ping["device_id"]] = ping
                host = ping["hostname"]
                if ping["timed_out"]:
                    # 在线但超时：记 timeout 指标（聚合层 3 轮防抖告警）
                    result.add(f"ping_timeout.{host}", 1.0, ts)
                elif ping["latency_ms"] is not None:
                    result.add(f"latency_ms.{host}", ping["latency_ms"], ts)
                    result.add(f"packet_loss_percent.{host}", ping["loss_percent"], ts)
        result.payload = {"devices": self._devices, "pings": dict(self._ping_cache),
                          "ts": ts, "backend_state": data.get("BackendState")}
        return result

    async def _mock_run(self) -> CollectorResult:
        return self._mock_result()
