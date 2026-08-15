"""Collector 基类 + 统一工具。

强制规范（评审 B）：
- 所有采集器必须 async；每步操作 asyncio.wait_for 硬超时（config.STEP_TIMEOUT）
- 同步阻塞调用（psutil 磁盘等）走 asyncio.to_thread
- 每个 collector 独立 try/except（scheduler 层兜底，单失败不影响其他）
- 统一 sanitize() 脱敏层（评审 F4/E6）：过滤公钥/密钥指纹等敏感字段
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from app import config

log = logging.getLogger(__name__)

# 脱敏关键词（字段名小写后子串匹配）——覆盖 tailscale status --json /
# headscale nodes list 中的公钥/密钥指纹字段
_SECRET_SUBSTRINGS = (
    "nodekey", "discokey", "machinekey", "publickey", "privatekey",
    "preauthkey", "authkey", "keyfingerprint", "fingerprint",
    "token", "password", "secret", "credential",
)


def sanitize(obj: Any, path: str = "") -> Any:
    """递归脱敏：命中敏感关键词的字段值替换为 <redacted>。

    应用于任何含公钥/密钥指纹的数据（日志与入库前）。
    """
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            if _is_secret_key(k):
                out[k] = "<redacted>"
            else:
                out[k] = sanitize(v, path + "." + k)
        return out
    if isinstance(obj, list):
        return [sanitize(v, path) for v in obj]
    return obj


def _is_secret_key(key: str) -> bool:
    # 归一化（去掉下划线）后匹配：同时覆盖 node_key / NodeKey / nodeKey 形态
    k = str(key).lower().replace("_", "").replace("-", "")
    return any(s in k for s in _SECRET_SUBSTRINGS)


class CollectorError(Exception):
    """采集失败（超时/数据源不可达等），由调度器记录，不中断其他采集器。"""


def validate_ip_or_host(value: str) -> bool:
    """tailscale ping 目标参数校验（评审 C5：防 sudo 注入）。"""
    return bool(re.fullmatch(r"[0-9a-fA-F:.-A-Za-z_]+", value)) and " " not in value


async def run_cmd(args: list[str], timeout: float | None = None,
                  sudo: bool = False) -> tuple[int, str, str]:
    """执行子进程（参数列表，不经 shell）。

    - sudo=True 时前缀 sudo -n（sudoers NOPASSWD 白名单）
    - 超时 proc.kill() + await wait()，防僵尸/句柄泄漏（评审 E4）
    返回 (returncode, stdout, stderr)。
    """
    timeout = timeout or config.STEP_TIMEOUT
    cmd = (["sudo", "-n"] if sudo else []) + list(args)
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()  # 确保收割
        raise CollectorError(f"cmd timeout after {timeout}s: {' '.join(cmd)}") from None
    return proc.returncode or 0, out.decode("utf-8", errors="replace"), \
        err.decode("utf-8", errors="replace")


@dataclass
class Metric:
    """单条样本：metric_name（含 <base>.<suffix> 形态）/ value / ts。"""
    name: str
    value: float
    ts: float | None = None  # None → 聚合层取当前时间


@dataclass
class CollectorResult:
    """规范化采集结果：主 group + 扩展 group 指标 + 富数据 payload（供 API 快照）。"""
    node_id: str
    group: str
    metrics: list[Metric] = field(default_factory=list)
    groups: dict[str, list[Metric]] = field(default_factory=dict)
    payload: dict[str, Any] | None = None

    def add(self, name: str, value: float, ts: float | None = None) -> None:
        self.metrics.append(Metric(name, value, ts))

    # ---- 快照序列化（MONITOR_MOCK=snapshot：静态真实数据本机测试）----
    def to_snapshot(self) -> dict:
        return {
            "node_id": self.node_id,
            "group": self.group,
            "metrics": [{"name": m.name, "value": m.value, "ts": m.ts}
                        for m in self.metrics],
            "groups": {k: [{"name": m.name, "value": m.value, "ts": m.ts}
                           for m in v] for k, v in self.groups.items()},
            "payload": self.payload,
        }

    @classmethod
    def from_snapshot(cls, data: dict) -> "CollectorResult":
        r = cls(node_id=data.get("node_id", "cloud-1"), group=data.get("group", ""))
        for m in data.get("metrics", []):
            r.add(m["name"], float(m["value"]), m.get("ts"))
        for g, ms in data.get("groups", {}).items():
            r.groups[g] = [Metric(m["name"], float(m["value"]), m.get("ts")) for m in ms]
        r.payload = data.get("payload")
        return r


class Collector:
    """采集器基类。

    子类实现 _mock_run()（模拟数据）与 _run_real()（真实采集）；
    run_once() 统一分派：snapshot 模式读 snapshots/<name>.json → mock → real。
    """

    name: str = "base"
    group: str = "base"
    interval: int = 30  # 秒（config.INTERVALS 同名键优先）

    def __init__(self, mock: bool | None = None) -> None:
        self.mock = config.MOCK if mock is None else mock

    async def run_once(self) -> CollectorResult:
        if config.SNAPSHOT:
            return self._snapshot_run()
        if self.mock:
            return await self._mock_run()
        return await self._run_real()

    async def _mock_run(self) -> CollectorResult:
        raise NotImplementedError

    async def _run_real(self) -> CollectorResult:
        raise NotImplementedError

    def _snapshot_run(self) -> CollectorResult:
        """读取 scripts/fetch_snapshot.sh 生成的真实数据快照。"""
        path = config.SNAPSHOT_DIR / f"{self.name}.json"
        if not path.exists():
            log.warning("snapshot: %s missing (%s), skip", self.name, path)
            return CollectorResult(node_id=config.NODE_ID, group=self.group)
        try:
            import json
            with open(path, encoding="utf-8") as f:
                return CollectorResult.from_snapshot(json.load(f))
        except Exception:  # noqa: BLE001
            log.exception("snapshot: %s load failed", self.name)
            return CollectorResult(node_id=config.NODE_ID, group=self.group)
