"""服务台账采集器（issue #2 方案 C：端口约定驱动自动发现）。

- 扫描目标：tailscale 在线设备（devices 表 online=1）+ 本机 127.0.0.1（device_id=local）
- 扫描范围：config.SERVICES_PORT_RANGE 约定区（默认 10000-19999），分片轮巡：
  每轮每设备扫 SERVICES_SCAN_BATCH 个端口，游标循环推进覆盖全量；
  已登记端口（db services 表）每轮必探，保持状态新鲜
- 探测：TCP 全连接（asyncio.open_connection，CONNECT_TIMEOUT，Semaphore 限并发）；
  协议识别：HTTP GET /（读 2KB，抓 status/title/Server 头）→ SSH banner → tcp
- 状态语义：连续 SERVICES_MISSING_ROUNDS 轮未开放 → status=closed（保留行置灰）
- 命名：SERVICES_ALIASES（ip:port 人工覆盖）> <title> > Server 头 > 设备名:端口
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time

from app import config, constants, db
from app.collectors.base import Collector, CollectorResult

log = logging.getLogger(__name__)

_TITLE_RE = re.compile(r"(?is)<title[^>]*>(.*?)</title>")
_SERVER_RE = re.compile(r"(?im)^server:\s*(.+)$")
_STATUS_RE = re.compile(r"HTTP/\d\.\d\s+(\d{3})")

# mock 确定性服务样例（对齐 docs/架构设计/服务台账与端口约定.md 示例）
_MOCK_SERVICES = [
    {"device_id": "local", "device_name": "localhost", "tailscale_ip": "127.0.0.1",
     "port": 10000, "protocol": "http", "auto_name": "本地服务预览", "http_status": 200,
     "url": "http://127.0.0.1:10000", "latency_ms": 1.2},
    {"device_id": "self-cloud-1", "device_name": "cloud-1", "tailscale_ip": "100.64.0.1",
     "port": 10001, "protocol": "http", "auto_name": "monitor-panel", "http_status": 200,
     "url": "http://100.64.0.1:10001", "latency_ms": 3.5},
    {"device_id": "self-cloud-1", "device_name": "cloud-1", "tailscale_ip": "100.64.0.1",
     "port": 10002, "protocol": "http", "auto_name": "旧监控面板", "http_status": 200,
     "url": "http://100.64.0.1:10002", "latency_ms": 4.1},
    {"device_id": "ts-dc-desktop", "device_name": "dc-desktop", "tailscale_ip": "100.64.0.2",
     "port": 10010, "protocol": "http", "auto_name": "摄像头", "http_status": 200,
     "url": "http://100.64.0.2:10010", "latency_ms": 12.8},
    {"device_id": "ts-dc-desktop", "device_name": "dc-desktop", "tailscale_ip": "100.64.0.2",
     "port": 10011, "protocol": "ssh", "auto_name": "dc-desktop:10011", "latency_ms": 10.2},
    {"device_id": "ts-oppo-pad-mini", "device_name": "oppo-pad-mini", "tailscale_ip": "100.64.0.4",
     "port": 10020, "protocol": "tcp", "auto_name": "oppo-pad-mini:10020", "latency_ms": 20.5},
]


class ServicesCollector(Collector):
    name = "services"
    group = constants.GROUP_SERVICES
    interval = 60

    def __init__(self, mock: bool | None = None) -> None:
        super().__init__(mock)
        lo, hi = config.SERVICES_PORT_RANGE
        self._port_lo = int(lo)
        self._port_hi = int(hi)
        self._cursor = self._port_lo          # 分片游标（每轮推进 SCAN_BATCH）
        self._missing: dict[tuple[str, int], int] = {}   # (device_id, port) → 连续未开放轮数
        self._known: dict[tuple[str, int], dict] = {}    # (device_id, port) → 上次识别信息
        self._sem: asyncio.Semaphore | None = None

    # ---------------- 公共 ----------------
    def _semaphore(self) -> asyncio.Semaphore:
        if self._sem is None:
            self._sem = asyncio.Semaphore(config.SERVICES_CONCURRENCY)
        return self._sem

    def _targets(self) -> list[dict]:
        """扫描目标：本机 + tailscale 在线设备（离线跳过，防超时放大）。"""
        targets = [{"device_id": "local", "device_name": "localhost",
                    "tailscale_ip": "127.0.0.1"}]
        for d in db.get_devices():
            if d.get("online") and d.get("tailscale_ip"):
                targets.append({"device_id": d["device_id"],
                                "device_name": d.get("name") or d["device_id"],
                                "tailscale_ip": d["tailscale_ip"]})
        return targets

    def _scope_ports(self, ip: str) -> list[int]:
        """从 terraform probe_scope.json 取该 IP 的公网声明端口（按 mtime 缓存）。

        格式：{"servers": {"cloud": {"host": "...", "public_tcp": ["22", "20000-20099"]}}}
        端口段 "20000-20099" 展开为区间。文件缺失/解析失败静默跳过（不影响现有探测）。
        """
        path = config.SERVICES_SCOPE_FILE
        if not path:
            return []
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            return []
        if getattr(self, "_scope_cache_mtime", None) != mtime:
            try:
                with open(path, encoding="utf-8") as fh:
                    data = json.load(fh)
                by_host: dict[str, list[int]] = {}
                for srv in (data.get("servers") or {}).values():
                    host = (srv or {}).get("host")
                    if not host:
                        continue
                    ports: list[int] = []
                    for item in (srv.get("public_tcp") or []):
                        txt = str(item)
                        if "-" in txt:
                            lo, _, hi = txt.partition("-")
                            try:
                                ports.extend(range(int(lo), int(hi) + 1))
                            except ValueError:
                                continue
                        elif txt.isdigit():
                            ports.append(int(txt))
                    by_host[host] = sorted(set(ports))
                self._scope_cache = by_host
                self._scope_cache_mtime = mtime
            except (OSError, ValueError, json.JSONDecodeError):
                log.warning("services: scope 文件解析失败 %s", path, exc_info=True)
                self._scope_cache = {}
                self._scope_cache_mtime = mtime
        return (getattr(self, "_scope_cache", {}) or {}).get(ip, [])

    def _extra_config(self, ip: str) -> dict[int, dict]:
        """该设备的 extra 端口配置：{port: options}。兼容 list 与 dict 两种形态。

        - list 形态（旧）：[22, 443] → 默认探测（Host=IP，明文 HTTP）
        - dict 形态（新）：{"443": {"host": "monitor.dc-sy.cn", "https": true,
                                    "path": "/"}} → 按域名探测（Host 头/SNI，可选 TLS）
        键匹配：精确 IP、"local"（本机 127.0.0.1）、"*"（所有设备）。
        """
        cfg = config.SERVICES_EXTRA_PORTS or {}
        out: dict[int, dict] = {}
        for key, val in cfg.items():
            if key == "*" or key == ip or (key == "local" and ip == "127.0.0.1"):
                if isinstance(val, list):
                    for p in val:
                        out.setdefault(int(p), {})
                elif isinstance(val, dict):
                    for ps, opts in val.items():
                        out.setdefault(int(ps),
                                       opts if isinstance(opts, dict) else {})
        return out

    def _extra_ports_for(self, ip: str) -> list[int]:
        return sorted(self._extra_config(ip).keys())

    def _probe_options(self, ip: str, port: int) -> dict:
        """端口探测选项：host（Host 头/SNI 域名）、https（TLS）、path（请求路径）。"""
        return self._extra_config(ip).get(port, {})

    def _ports_for(self, target: dict,
                   registered: dict[tuple[str, int], dict]) -> list[int]:
        """本轮端口集合：分片（游标推进）+ 已登记端口必探 + 约定区外 extra 必探。"""
        device_id = target["device_id"]
        batch = max(1, config.SERVICES_SCAN_BATCH)
        chunk = list(range(self._cursor,
                           min(self._cursor + batch, self._port_hi + 1)))
        known = [port for (did, port) in registered
                 if did == device_id and port not in chunk]
        extra = [p for p in self._extra_ports_for(target["tailscale_ip"])
                 if p not in chunk and p not in known]
        # terraform 声明集（公网视角）：已放行端口也纳入必探，避免"声明了却不探"
        scope = [p for p in self._scope_ports(target["tailscale_ip"])
                 if p not in chunk and p not in known and p not in extra]
        return chunk + known + extra + scope

    def _advance_cursor(self) -> None:
        self._cursor += max(1, config.SERVICES_SCAN_BATCH)
        if self._cursor > self._port_hi:
            self._cursor = self._port_lo

    # ---------------- mock（确定性，测试稳定） ----------------
    def _mock_result(self) -> CollectorResult:
        ts = time.time()
        result = CollectorResult(node_id=config.NODE_ID, group=self.group)
        rows = []
        for s in _MOCK_SERVICES:
            key = (s["device_id"], s["port"])
            known = self._known.get(key, {})
            row = {
                **s,
                "name": config.SERVICES_ALIASES.get(f"{s['tailscale_ip']}:{s['port']}"),
                "status": "open", "missing_rounds": 0, "source": "auto",
                "first_seen_ts": known.get("first_seen_ts", ts),
                "last_seen_ts": ts, "updated_ts": ts, "mock": True,
            }
            self._known[key] = row
            rows.append(row)
            result.add(f"latency_ms.{s['tailscale_ip']}:{s['port']}", s["latency_ms"], ts)
            result.add(f"reachable.{s['tailscale_ip']}:{s['port']}", 1.0, ts)
        result.payload = {"services": rows, "range": [self._port_lo, self._port_hi],
                          "ts": ts, "mock": True}
        return result

    # ---------------- real 探测 ----------------
    async def _probe(self, target: dict, port: int) -> dict | None:
        """TCP 连接 + 协议识别。返回 None=端口未开放。

        探测选项（extra 配置 dict 形态）：host=Host 头/SNI 域名（默认 IP）、
        https=True 走 TLS（证书不校验，探测目的）、path=请求路径（默认 /）。
        """
        import ssl

        ip = target["tailscale_ip"]
        opts = self._probe_options(ip, port)
        host = opts.get("host") or ip
        https = bool(opts.get("https"))
        path = opts.get("path") or "/"
        started = time.monotonic()
        try:
            async with self._semaphore():
                if https:
                    ctx = ssl.create_default_context()
                    ctx.check_hostname = False
                    ctx.verify_mode = ssl.CERT_NONE
                    reader, writer = await asyncio.wait_for(
                        asyncio.open_connection(ip, port, ssl=ctx,
                                                server_hostname=host),
                        timeout=config.SERVICES_CONNECT_TIMEOUT)
                else:
                    reader, writer = await asyncio.wait_for(
                        asyncio.open_connection(ip, port),
                        timeout=config.SERVICES_CONNECT_TIMEOUT)
        except Exception:  # noqa: BLE001 —— 未开放/拒绝/超时
            return None
        latency = (time.monotonic() - started) * 1000
        protocol = "tcp"
        http_status = None
        url = None
        title = None
        server = None
        try:
            writer.write(f"GET {path} HTTP/1.0\r\nHost: {host}\r\n\r\n".encode("utf-8"))
            data = await asyncio.wait_for(reader.read(2048),
                                          timeout=config.SERVICES_HTTP_TIMEOUT)
            head = data[:2048].decode("utf-8", errors="replace")
            if "HTTP/" in head[:24]:
                protocol = "http"
                m = _STATUS_RE.search(head)
                http_status = int(m.group(1)) if m else None
                m = _SERVER_RE.search(head)
                server = m.group(1).strip() if m else None
                m = _TITLE_RE.search(head)
                title = m.group(1).strip() if m else None
                scheme = "https" if https else "http"
                url = f"{scheme}://{host}:{port}{path}"
            elif head.startswith("SSH-"):
                # sshd 主动 banner，首读即可判定（无需二次读）
                protocol = "ssh"
        except Exception:  # noqa: BLE001 —— 非 HTTP（读超时/EOF/协议错）
            pass
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass
        key_known = self._known.get((target["device_id"], port), {})
        if protocol == "http":
            auto_name = (title or "").strip()[:40] or (server or "").strip()[:40] \
                or f"{target['device_name']}:{port}"
        else:
            auto_name = key_known.get("auto_name") or f"{target['device_name']}:{port}"
        return {
            "device_id": target["device_id"], "device_name": target["device_name"],
            "tailscale_ip": ip, "port": port, "protocol": protocol,
            "auto_name": auto_name, "http_status": http_status, "url": url,
            "latency_ms": round(latency, 1),
            "name": config.SERVICES_ALIASES.get(f"{ip}:{port}"),
        }

    async def _run_real(self) -> CollectorResult:
        ts = time.time()
        result = CollectorResult(node_id=config.NODE_ID, group=self.group)
        targets = self._targets()
        registered_rows = db.get_services()
        # agent 行（source='agent'）由 agent 独占维护：中心采集器不探测、不重写，
        # 否则会用 node_id=本机 覆盖成第二行（同 device+port 重复）。
        agent_owned = {(r["device_id"], r["port"]) for r in registered_rows
                       if r.get("source") == "agent"}
        registered = {(r["device_id"], r["port"]): r for r in registered_rows
                      if r.get("source") != "agent" and (r.get("protocol") or "tcp") == "tcp"}

        rows: list[dict] = []
        for target in targets:
            dev_id = target["device_id"]
            ports = [p for p in self._ports_for(target, registered)
                     if (dev_id, p) not in agent_owned]
            probes = await asyncio.gather(*[self._probe(target, p) for p in ports],
                                          return_exceptions=True)
            for port, r in zip(ports, probes):
                if isinstance(r, Exception):
                    log.warning("services: probe %s:%s failed: %s", dev_id, port, r)
                    r = None
                key = (dev_id, port)
                if r is not None:  # 开放
                    self._missing[key] = 0
                    known = self._known.get(key, {})
                    prev = registered.get(key) or {}
                    row = {
                        **r,
                        "status": "open", "missing_rounds": 0,
                        # source/enabled 保持库中原值（manual/agent 行不降级为 auto）
                        "source": prev.get("source", "auto"),
                        "enabled": prev["enabled"] if prev.get("enabled") is not None else 1,
                        "first_seen_ts": known.get("first_seen_ts", ts),
                        "last_seen_ts": ts, "updated_ts": ts,
                    }
                    self._known[key] = row
                    rows.append(row)
                    result.add(f"latency_ms.{target['tailscale_ip']}:{port}",
                               row["latency_ms"], ts)
                    result.add(f"reachable.{target['tailscale_ip']}:{port}", 1.0, ts)
                elif key in registered:
                    # 已登记但本轮未开放：missing 计数，达阈值置 closed（保留行置灰）
                    miss = self._missing.get(key, 0) + 1
                    self._missing[key] = miss
                    prev = registered[key]
                    status = ("closed" if miss >= config.SERVICES_MISSING_ROUNDS
                              else prev.get("status") or "closed")
                    row = {
                        "device_id": dev_id,
                        "device_name": prev.get("device_name") or target["device_name"],
                        "tailscale_ip": prev.get("tailscale_ip") or target["tailscale_ip"],
                        "port": port,
                        "protocol": prev.get("protocol") or "tcp",
                        "auto_name": prev.get("auto_name") or f"{target['device_name']}:{port}",
                        "name": prev.get("name"),
                        "status": status, "missing_rounds": miss,
                        "source": prev.get("source", "auto"),
                        "last_seen_ts": prev.get("last_seen_ts"),
                        "updated_ts": ts,
                    }
                    rows.append(row)
                    result.add(f"reachable.{target['tailscale_ip']}:{port}", 0.0, ts)
        self._advance_cursor()
        result.payload = {"services": rows, "range": [self._port_lo, self._port_hi],
                          "ts": ts, "mock": False}
        return result

    async def _mock_run(self) -> CollectorResult:
        return self._mock_result()
