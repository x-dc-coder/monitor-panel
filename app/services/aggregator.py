"""聚合层：最新结果 → 内存快照 + samples 入库 + 阈值判定 + 告警流。

评审硬要求落实：
- ping 超时语义（F5/E3）：设备在线但 ping 超时 → warn（连续 3 轮防抖）；
  离线 → 展示不告警（防休眠设备误报）
- 告警冷却（8899 模式复用）：同类 (source, message) 30min 去重；条件恢复自动 resolved
- frps 流量计数重启归零（E17）：samples 存原始值，不做 delta（前端对骤降提示）
"""
from __future__ import annotations

import asyncio
import logging
import time

from app import config, constants, db
from app.collectors.base import CollectorResult, Metric

log = logging.getLogger(__name__)


class Aggregator:
    def __init__(self) -> None:
        self.latest: dict[tuple[str, str], dict] = {}      # (node_id, group) → 最新 payload（API 读一致快照）
        self.latest_ts: dict[tuple[str, str], float] = {}
        self._ping_streak: dict[str, int] = {}  # 设备 → 连续超时轮数（防抖）

    def latest_for(self, group: str, node_id: str | None = None) -> dict:
        nid = node_id or config.NODE_ID
        return self.latest.get((nid, group), {})

    # ---------------- 入口 ----------------
    async def ingest(self, result: CollectorResult) -> None:
        await asyncio.to_thread(self._ingest_blocking, result, time.time())

    def _ingest_blocking(self, result: CollectorResult, ts: float) -> None:
        try:
            # 1) samples：主 group + 扩展 group，一次批量事务
            groups = [(result.group, result.metrics)] + list(result.groups.items())
            for group, metrics in groups:
                # 兼容 Metric 对象与 (name, value, ts) 元组两种形态
                rows = []
                for m in metrics:
                    if isinstance(m, Metric):
                        rows.append((m.name, m.value, m.ts or ts))
                    else:
                        # 显式括号：3 元组缺省 ts 回退 now；2 元组直接用 now
                        rows.append((m[0], m[1], (m[2] or ts) if len(m) > 2 else ts))
                db.insert_samples(result.node_id, group, rows)

            # 2) 快照缓存
            self.latest_ts[(result.node_id, result.group)] = ts
            if result.payload is not None:
                self.latest[(result.node_id, result.group)] = result.payload

            # 3) 各 group 专用处理（快照表/阈值/告警）——主 group + 扩展 group
            groups_handled = set()
            handler = getattr(self, f"_on_{result.group}", None)
            if handler is not None:
                groups_handled.add(result.group)
                handler(result, ts)
            for group in result.groups:
                if group in groups_handled:
                    continue
                h = getattr(self, f"_on_{group}", None)
                if h is not None:
                    groups_handled.add(group)
                    h(result, ts)
        except Exception:  # noqa: BLE001 —— 聚合失败不中断调度
            log.exception("aggregator: ingest %s failed", result.group)

    # ---------------- tailscale（group=ts_latency） ----------------
    def _on_ts_latency(self, result: CollectorResult, ts: float) -> None:
        payload = result.payload or {}
        devices = payload.get("devices", [])
        if devices:
            db.upsert_devices(result.node_id, devices)
        # 设备密钥有效期（KeyExpiry）预警：离线设备不判（先例：防休眠误报）
        for dev in devices:
            self._check_key_expiry(dev)
        # ping 结果：在线+超时防抖告警；离线清除状态不告警
        for dev in devices:
            dev_id = dev.get("device_id")
            if not dev_id:
                continue
            if not dev.get("online"):
                self._ping_streak.pop(dev_id, None)
                continue
            ping = (payload.get("pings") or {}).get(dev_id)
            if not ping:
                continue
            host = ping.get("hostname") or dev.get("name") or dev_id
            if ping.get("timed_out"):
                streak = self._ping_streak.get(dev_id, 0) + 1
                self._ping_streak[dev_id] = streak
                if streak >= config.PING_TIMEOUT_DEBOUNCE_ROUNDS:
                    self._alert(
                        constants.LEVEL_WARN, f"ts:{host}",
                        f"{host} 在线但 tailscale ping 超时（连续 {streak} 轮）")
            else:
                self._ping_streak[dev_id] = 0
                db.resolve_alerts(f"ts:{host}")
                # 延迟/丢包阈值
                lat = ping.get("latency_ms")
                loss = ping.get("loss_percent") or 0.0
                if lat is not None:
                    if lat > config.THRESHOLDS["ts_latency_crit_ms"]:
                        self._alert(constants.LEVEL_CRIT, f"ts:{host}",
                                    f"{host} 延迟 {lat}ms 超过严重阈值 "
                                    f"{config.THRESHOLDS['ts_latency_crit_ms']}ms")
                    elif lat > config.THRESHOLDS["ts_latency_warn_ms"]:
                        self._alert(constants.LEVEL_WARN, f"ts:{host}",
                                    f"{host} 延迟 {lat}ms 超过警告阈值 "
                                    f"{config.THRESHOLDS['ts_latency_warn_ms']}ms")
                    else:
                        db.resolve_alerts(f"ts:{host}")
                if loss > config.THRESHOLDS["packet_loss_crit_pct"]:
                    self._alert(constants.LEVEL_CRIT, f"ts:{host}",
                                f"{host} 丢包 {loss}% 超过严重阈值")
                elif loss > config.THRESHOLDS["packet_loss_warn_pct"]:
                    self._alert(constants.LEVEL_WARN, f"ts:{host}",
                                f"{host} 丢包 {loss}% 超过警告阈值")
                else:
                    db.resolve_alerts(f"ts:{host}")

    def _check_key_expiry(self, dev: dict) -> None:
        """KeyExpiry 预警：<14d warn、<3d crit、已过期 crit；正常/未知自动 resolve。"""
        host = dev.get("name") or dev.get("device_id")
        src = f"ts_key:{host}"
        days = dev.get("key_days_left")
        if not dev.get("online") or days is None:
            db.resolve_alerts(src)
            return
        if days <= 0:
            self._alert(constants.LEVEL_CRIT, src,
                        f"{host} tailscale 密钥已过期 {abs(days):.0f} 天（设备将无法重连）")
        elif days < config.THRESHOLDS["ts_key_crit_days"]:
            self._alert(constants.LEVEL_CRIT, src,
                        f"{host} tailscale 密钥 {days:.0f} 天后过期")
        elif days < config.THRESHOLDS["ts_key_warn_days"]:
            self._alert(constants.LEVEL_WARN, src,
                        f"{host} tailscale 密钥 {days:.0f} 天后过期，请预授权续期")
        else:
            db.resolve_alerts(src)

    # ---------------- frp ----------------
    def _on_frp_traffic(self, result: CollectorResult, ts: float) -> None:
        payload = result.payload or {}
        tunnels = payload.get("tunnels", [])
        if tunnels:
            rows = []
            for t in tunnels:
                rows.append({
                    "node_id": result.node_id, "tunnel_id": t["tunnel_id"],
                    "type": t.get("type"), "remote_port": t.get("remote_port"),
                    "local_addr": t.get("local_addr"),
                    "status": t.get("status", "unknown"),
                    "traffic_in": t.get("traffic_in", 0) or 0,
                    "traffic_out": t.get("traffic_out", 0) or 0,
                    "updated_ts": ts,
                })
            db.upsert_tunnels(result.node_id, rows)
        # 隧道离线即 warn（阈值默认）；恢复自动 resolved
        for t in tunnels:
            tid = t.get("tunnel_id")
            if not tid:
                continue
            src = f"frp_tunnel:{tid}"
            if t.get("status") != "online":
                self._alert(constants.LEVEL_WARN, src, f"frp 隧道 {tid} 离线")
            else:
                db.resolve_alerts(src)

    def _on_frp_link(self, result: CollectorResult, ts: float) -> None:
        """8899 链路质量阈值（F3）。数据在 payload['link']。"""
        link = (result.payload or {}).get("link") or {}
        for dev, d in link.items():
            src = f"frp_link:{dev}"
            lat = d.get("latency_ms")
            loss = d.get("packet_loss_percent")
            alerted = False
            if lat is not None and lat > config.THRESHOLDS["ts_latency_warn_ms"]:
                lvl = constants.LEVEL_CRIT if lat > config.THRESHOLDS["ts_latency_crit_ms"]                     else constants.LEVEL_WARN
                self._alert(lvl, src, f"frp 链路 {dev} 延迟 {lat}ms 异常")
                alerted = True
            if loss is not None and loss > config.THRESHOLDS["packet_loss_warn_pct"]:
                lvl = constants.LEVEL_CRIT if loss > config.THRESHOLDS["packet_loss_crit_pct"]                     else constants.LEVEL_WARN
                self._alert(lvl, src, f"frp 链路 {dev} 丢包 {loss}% 异常")
                alerted = True
            # 仅当两项数据都存在且均未超阈值时才 resolve；数据缺失（None）不判定
            if not alerted and lat is not None and loss is not None:
                db.resolve_alerts(src)

    # ---------------- system / certs / headscale / network ----------------
    def _on_system(self, result: CollectorResult, ts: float) -> None:
        disk = (result.payload or {}).get("disk") or {}
        for mount, info in disk.items():
            pct = info.get("percent", 0.0)
            src = f"disk:{mount}"
            if pct >= config.THRESHOLDS["disk_crit_pct"]:
                self._alert(constants.LEVEL_CRIT, src, f"磁盘 {mount} 使用率 {pct}% 严重")
            elif pct >= config.THRESHOLDS["disk_warn_pct"]:
                self._alert(constants.LEVEL_WARN, src, f"磁盘 {mount} 使用率 {pct}% 超阈值")
            else:
                db.resolve_alerts(src)

    def _on_cert_days(self, result: CollectorResult, ts: float) -> None:
        certs = (result.payload or {}).get("certs") or []
        for c in certs:
            domain = c.get("domain")
            if not domain:
                continue
            src = f"cert:{domain}"
            error = c.get("error")
            days = c.get("days_left")
            if error or c.get("status") == constants.LEVEL_CRIT or                (days is not None and days < config.THRESHOLDS["cert_crit_days"]):
                if error:
                    msg = f"证书 {domain} 探测失败：{error}"
                elif days is None:
                    # status=CRIT 但天数缺失（远端上报缺字段）→ 不得格式化 None
                    msg = f"证书 {domain} 状态严重（剩余天数未知）"
                else:
                    msg = f"证书 {domain} 剩余 {days:.0f} 天，即将过期"
                self._alert(constants.LEVEL_CRIT, src, msg)
            elif days is not None and days < config.THRESHOLDS["cert_warn_days"]:
                self._alert(constants.LEVEL_WARN, src,
                            f"证书 {domain} 剩余 {days:.0f} 天")
            else:
                db.resolve_alerts(src)

    def _on_headscale(self, result: CollectorResult, ts: float) -> None:
        vals = (result.payload or {}).get("values") or {}
        if vals.get("tcp_ok", 1) == 0:
            self._alert(constants.LEVEL_WARN, "headscale:tcp",
                        "headscale 控制面 8080 TCP 探活失败")
        else:
            db.resolve_alerts("headscale:tcp")

    def _on_network(self, result: CollectorResult, ts: float) -> None:
        if not config.NETWORK_PROBE_ALERT:
            return
        probes = (result.payload or {}).get("probes") or []
        for p in probes:
            src = f"probe:{p.get('name')}"
            if not p.get("reachable"):
                self._alert(constants.LEVEL_WARN, src,
                            f"公网探测 {p.get('name')}（{p.get('target')}）不可达")
            else:
                db.resolve_alerts(src)

    # ---------------- services（服务台账：payload → services 表 + 下线告警 + 行生命周期） ----------------
    @staticmethod
    def _extra_ports_by_ip() -> dict[str, set[int]]:
        """extra 配置展开：探测 IP → 端口集合（含 "*" 键并入所有 IP，local 键映射 127.0.0.1）。"""
        out: dict[str, set[int]] = {}
        star: set[int] = set()
        for key, val in (config.SERVICES_EXTRA_PORTS or {}).items():
            ports: set[int] = set()
            if isinstance(val, list):
                ports = {int(p) for p in val}
            elif isinstance(val, dict):
                ports = {int(p) for p in val}
            if key == "*":
                star |= ports
            else:
                ip = "127.0.0.1" if key == "local" else key
                out.setdefault(ip, set()).update(ports)
        if star:
            for ips in out.values():
                ips |= star
            out.setdefault("*", star)
        return out

    def _prune_services(self, ts: float) -> int:
        """行生命周期（2026-08-24 审查：治理 1199 条未解决误报的根源）。

        删除条件（仅 source=auto；manual 人工登记与 agent 自报行永不删）：
        1. 端口既不在约定区、也不在该设备 extra 配置里 → 历史登记残留
           （extra 已移除的端口行曾"已登记必探"永生 → 永久 closed 周期告警）
        2. 约定区内 closed 且距最后在线（last_seen_ts）> SERVICES_PRUNE_CLOSED_S
           → 手机 ephemeral / 真下线服务；不用 updated_ts（closed 行每轮
           探测都会刷新它，会让 prune 永不触发）
        agent 行的生命周期由 agent 上报的替换语义负责，中心采集器只探测不删除。
        删除行时同步 resolve 对应告警（历史误报一并清理）。
        """
        lo, hi = config.SERVICES_PORT_RANGE
        extra = self._extra_ports_by_ip()
        star = extra.get("*", set())
        doomed: dict[int, str] = {}   # row id → alert source
        for row in db.get_services():
            if row.get("source") in ("manual", "agent"):
                continue
            port = row.get("port")
            if port is None:
                continue
            ip = row.get("tailscale_ip") or ""
            in_extra = port in extra.get(ip, set()) or port in star
            stale_extra = not (lo <= port <= hi) and not in_extra
            last_seen = row.get("last_seen_ts") or row.get("updated_ts")
            stale_closed = (lo <= port <= hi and row.get("status") == "closed"
                            and last_seen
                            and ts - last_seen > config.SERVICES_PRUNE_CLOSED_S)
            if stale_extra or stale_closed:
                doomed[row["id"]] = f"service:{ip}:{port}"
        if not doomed:
            return 0
        for src in doomed.values():
            db.resolve_alerts(src)
        n = db.delete_services(list(doomed))
        if n:
            log.info("services: pruned %d stale row(s)", n)
        return n

    def _on_services(self, result: CollectorResult, ts: float) -> None:
        rows = (result.payload or {}).get("services") or []
        if rows:
            db.upsert_services(result.node_id, rows)
        # 设备在线状态（离线设备的服务"下线"多因休眠——抑制告警，tailscale ping 先例）
        offline = {d["device_id"] for d in db.get_devices() if not d.get("online")}
        for row in rows:
            ip = row.get("tailscale_ip") or ""
            port = row.get("port")
            if not port:
                continue
            src = f"service:{ip}:{port}"
            name = row.get("name") or row.get("auto_name") or f"{ip}:{port}"
            if row.get("status") == "closed":
                if row.get("device_id") in offline:
                    continue                      # 设备离线（休眠）→ 不告警
                first = row.get("first_seen_ts")
                if first and ts - first < config.SERVICES_ALERT_MIN_AGE_S:
                    continue                      # 存活 <10min 的临时端口 → 不告警
                # 连续 N 轮未开放才置 closed（采集器已防抖），此处补主动告警
                self._alert(constants.LEVEL_WARN, src, f"服务 {name}（{ip}:{port}）下线")
            elif row.get("status") == "open":
                db.resolve_alerts(src)
        self._prune_services(ts)

    # ---------------- listen_ports（agent 自报本机监听端口） ----------------
    @staticmethod
    def _tailscale_ip_for(node_id: str) -> str | None:
        """按节点名查 devices 表：device_id/name/MagicDNS 短名任一命中即取 IP。"""
        return (Aggregator._device_for(node_id) or {}).get("tailscale_ip")

    @staticmethod
    def _device_for(node_id: str) -> dict | None:
        """按节点名查 devices 表行：device_id/name/MagicDNS 短名任一命中。

        agent 行的 services.device_id 必须解析为 devices.device_id（而非
        node_id 字面值）——中心采集器「已登记必探」按 (devices.device_id,
        port) 键匹配，键不一致则 agent 端口永无真实探测。
        """
        for d in db.get_devices():
            dns_short = (d.get("dns_name") or "").split(".", 1)[0]
            if node_id in (d.get("device_id"), d.get("name"), dns_short):
                return d
        return None

    def _on_listen_ports(self, result: CollectorResult, ts: float) -> None:
        """agent 自报监听端口 → services 行（source='agent'）+ 替换语义。

        agent 是"本机在监听"的唯一权威：本轮清单即该节点 agent 行的全集，
        清单外的旧 agent 行删除；manual 行由人工维护，永不删除/覆盖。
        open/closed 仍由中心 services 采集器探测归因（防火墙拦截 = 监听但连不上）。
        """
        payload = result.payload or {}
        if "ports" not in payload:
            return
        existing = {(r.get("protocol") or "tcp", r["port"]): r
                    for r in db.get_services(node_id=result.node_id)}
        dev = self._device_for(result.node_id)
        if dev is not None:
            device_id = dev["device_id"]
            device_name = dev.get("name") or result.node_id
            ip = dev.get("tailscale_ip")
        elif result.node_id == config.NODE_ID:
            # 本机自身不在 devices 表（无 tailscale 身份）时走 local 目标约定
            device_id, device_name, ip = "local", "localhost", "127.0.0.1"
        else:
            device_id = device_name = result.node_id
            ip = self._tailscale_ip_for(result.node_id)
        seen: set[tuple[str, int]] = set()
        rows: list[dict] = []
        for item in payload.get("ports") or []:
            if not isinstance(item, dict):
                continue
            try:
                port = int(item["port"])
            except (KeyError, TypeError, ValueError):
                continue
            proto = item.get("proto") or "tcp"
            if port <= 0 or port > 65535 or (proto, port) in seen:
                continue
            seen.add((proto, port))
            prev = existing.get((proto, port)) or {}
            rows.append({
                "device_id": device_id,
                "device_name": device_name,
                "tailscale_ip": ip,
                "port": port,
                "protocol": proto,
                "name": None,
                "auto_name": prev.get("auto_name") or f"{result.node_id}:{port}",
                "status": prev.get("status") or "open",
                "enabled": prev.get("enabled", 1),
                "source": "agent",
                "first_seen_ts": prev.get("first_seen_ts") or ts,
                "last_seen_ts": ts,
                "updated_ts": ts,
            })
        if rows:
            db.upsert_services(result.node_id, rows)

        # 替换语义：只删本节点 source='agent' 且不在本轮清单中的行
        for gone in db.delete_agent_services_except(result.node_id, seen):
            db.resolve_alerts(
                f"service:{gone.get('tailscale_ip') or ''}:{gone['port']}")

        # handler 在 samples 批量写入之后执行；agent 未随 metrics 上报时补写计数
        has_count = any((m.name if isinstance(m, Metric) else m[0]) == "listening_count"
                        for m in result.metrics)
        if not has_count:
            db.insert_samples(result.node_id, constants.GROUP_LISTEN_PORTS,
                              [("listening_count", len(rows), ts)])
        result.add("listening_count", len(rows), ts)

    # ---------------- grm（Release 监控：payload → grm_repos 表，只读聚合） ----------------
    def _on_grm(self, result: CollectorResult, ts: float) -> None:
        payload = result.payload or {}
        if payload.get("degraded"):
            # 数据源不可读：保留上一轮快照不清空、不告警刷屏（frp 降级先例）
            return
        rows = payload.get("repos") or []
        db.upsert_grm_repos(result.node_id, rows)
        # Phase 3：同步写入统一 update_items（grm 为首个 UpdateProvider）
        # 非降级轮次无条件 prune：空清单 = grm 侧零仓库 → 清空该 node 全部行
        from app.collectors.updates import grm as grm_provider
        items = grm_provider.rows_to_items(result.node_id, rows, ts)
        db.upsert_update_items(result.node_id, items, prune=True)

    # ---------------- 告警 ----------------
    @staticmethod
    def _alert(level: str, source: str, message: str) -> None:
        if db.insert_alert(level, source, message):
            log.warning("alert[%s] %s: %s", level, source, message)

    # ---------------- 总览聚合（/api/overview 主轮询点） ----------------
    def overview(self, node_id: str | None = None) -> dict:
        nid = node_id or config.NODE_ID
        frp = self.latest_for(constants.GROUP_FRP_TRAFFIC, nid)
        tunnels = frp.get("tunnels", [])
        serverinfo = frp.get("serverinfo") or {}
        ts_p = self.latest_for(constants.GROUP_TS_LATENCY, nid)
        devices = ts_p.get("devices", [])
        sys_p = self.latest_for(constants.GROUP_SYSTEM, nid)
        cert_p = self.latest_for(constants.GROUP_CERT_DAYS, nid)
        certs = cert_p.get("certs", [])
        hs_p = self.latest_for(constants.GROUP_HEADSCALE, nid)
        net_p = self.latest_for(constants.GROUP_NETWORK, nid)
        probes = net_p.get("probes", [])
        grm_p = self.latest_for(constants.GROUP_GRM, nid)
        lab_p = self.latest_for(constants.GROUP_LAB, nid)
        node_ts = [v for (n, _), v in self.latest_ts.items() if n == nid]
        updated = max(node_ts, default=None)
        return {
            "mock": config.MOCK,
            "mode": "snapshot" if config.SNAPSHOT else ("mock" if config.MOCK else "live"),
            "updated_at": updated,
            "frp": {
                "total": len(tunnels),
                "online": sum(1 for t in tunnels if t.get("status") == "online"),
                "traffic_in": serverinfo.get("total_traffic_in"),
                "traffic_out": serverinfo.get("total_traffic_out"),
                "degraded": bool(frp.get("degraded")),
                "proxy_mode": frp.get("proxy_mode", "mock" if config.MOCK else None),
                "error": frp.get("error"),
            },
            "tailscale": {
                "total": len(devices),
                "online": sum(1 for d in devices if d.get("online")),
                "backend_state": ts_p.get("backend_state"),
                "any_derp": any(bool(d.get("relay")) for d in devices),
            },
            "system": {
                "cpu_percent": sys_p.get("cpu_percent"),
                "mem_percent": sys_p.get("mem_percent"),
                "load": sys_p.get("load"),
                "net": sys_p.get("net"),
                "disk": sys_p.get("disk"),
            },
            "headscale": {
                "tcp_ok": (hs_p.get("values") or {}).get("tcp_ok"),
                "nodes_total": (hs_p.get("values") or {}).get("nodes_total"),
                "derp_region": hs_p.get("derp_region"),
            },
            "certs": {
                "total": len(certs),
                "warn": sum(1 for c in certs if c.get("status") == constants.LEVEL_WARN),
                "crit": sum(1 for c in certs if c.get("status") == constants.LEVEL_CRIT),
                "expiring": sorted(certs, key=lambda c: c.get("days_left", 1e9))[:3],
            },
            "network": {
                "total": len(probes),
                "ok": sum(1 for p in probes if p.get("reachable")),
                "fail": sum(1 for p in probes if not p.get("reachable")),
            },
            "grm": {
                "total": (grm_p.get("summary") or {}).get("total", 0),
                "with_release": (grm_p.get("summary") or {}).get("with_release", 0),
                "new_release": (grm_p.get("summary") or {}).get("new_release", 0),
                "degraded": bool(grm_p.get("degraded")),
                "error": grm_p.get("error"),
                "base_url": grm_p.get("base_url"),
            },
            "lab": {
                "gpu_util_percent": lab_p.get("gpu_util_percent"),
                "vram_used_mib": lab_p.get("vram_used_mib"),
                "vram_total_mib": lab_p.get("vram_total_mib"),
                "vram_util_percent": lab_p.get("vram_util_percent"),
                "gpu_temp_c": lab_p.get("gpu_temp_c"),
                "cpu_percent": lab_p.get("cpu_percent"),
                "mem_percent": lab_p.get("mem_percent"),
                "mem_used_mib": lab_p.get("mem_used_mib"),
                "mem_total_mib": lab_p.get("mem_total_mib"),
                "experiment_active": lab_p.get("experiment_active"),
                "experiments_total": lab_p.get("experiments_total"),
                "alerts_crit": lab_p.get("alerts_crit"),
                "alerts_warn": lab_p.get("alerts_warn"),
                "monitor_enabled": lab_p.get("monitor_enabled"),
                "experiment": lab_p.get("experiment"),
                "procs_top": lab_p.get("procs_top") or [],
                "tags": lab_p.get("tags") or [],
            },
            "alerts": {"unresolved": db.count_unresolved_alerts()},
            "nodes": [
                {"id": n["id"], "role": n.get("role"),
                 "last_seen_ts": n.get("last_seen_ts")}
                for n in db.get_nodes()
            ],
        }
