"""聚合层：最新结果 → 内存快照 + samples 入库 + 阈值判定 + 告警流。

评审硬要求落实：
- ping 超时语义（F5/E3）：设备在线但 ping 超时 → warn（连续 3 轮防抖）；
  离线 → 展示不告警（防休眠设备误报）
- 告警冷却（8899 模式复用）：同类 (source, message) 30min 去重；条件恢复自动 resolved
- frps 流量计数重启归零（E17）：samples 存原始值，不做 delta（前端对骤降提示）
"""
from __future__ import annotations

import logging
import time

from app import config, constants, db
from app.collectors.base import CollectorResult, Metric

log = logging.getLogger(__name__)


class Aggregator:
    def __init__(self) -> None:
        self.latest: dict[str, dict] = {}      # group → 最新 payload（API 读一致快照）
        self.latest_ts: dict[str, float] = {}
        self._ping_streak: dict[str, int] = {}  # 设备 → 连续超时轮数（防抖）

    # ---------------- 入口 ----------------
    async def ingest(self, result: CollectorResult) -> None:
        ts = time.time()
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
                        rows.append((m[0], m[1], m[2] or ts if len(m) > 2 else ts))
                db.insert_samples(result.node_id, group, rows)

            # 2) 快照缓存
            self.latest_ts[result.group] = ts
            if result.payload is not None:
                self.latest[result.group] = result.payload

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
            days = c.get("days_left")
            if days is None or not domain:
                continue
            src = f"cert:{domain}"
            if days < config.THRESHOLDS["cert_crit_days"]:
                self._alert(constants.LEVEL_CRIT, src,
                            f"证书 {domain} 剩余 {days:.0f} 天，即将过期")
            elif days < config.THRESHOLDS["cert_warn_days"]:
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

        删除条件（仅 source=auto；manual 人工登记永不删）：
        1. 端口既不在约定区、也不在该设备 extra 配置里 → 历史登记残留
           （extra 已移除的端口行曾"已登记必探"永生 → 永久 closed 周期告警）
        2. 约定区内 closed 且距最后在线（last_seen_ts）> SERVICES_PRUNE_CLOSED_S
           → 手机 ephemeral / 真下线服务；不用 updated_ts（closed 行每轮
           探测都会刷新它，会让 prune 永不触发）
        删除行时同步 resolve 对应告警（历史误报一并清理）。
        """
        lo, hi = config.SERVICES_PORT_RANGE
        extra = self._extra_ports_by_ip()
        star = extra.get("*", set())
        doomed: dict[int, str] = {}   # row id → alert source
        for row in db.get_services():
            if row.get("source") == "manual":
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

    # ---------------- grm（Release 监控：payload → grm_repos 表，只读聚合） ----------------
    def _on_grm(self, result: CollectorResult, ts: float) -> None:
        payload = result.payload or {}
        if payload.get("degraded"):
            # 数据源不可读：保留上一轮快照不清空、不告警刷屏（frp 降级先例）
            return
        db.upsert_grm_repos(result.node_id, payload.get("repos") or [])

    # ---------------- 告警 ----------------
    @staticmethod
    def _alert(level: str, source: str, message: str) -> None:
        if db.insert_alert(level, source, message):
            log.warning("alert[%s] %s: %s", level, source, message)

    # ---------------- 总览聚合（/api/overview 主轮询点） ----------------
    def overview(self) -> dict:
        frp = self.latest.get(constants.GROUP_FRP_TRAFFIC, {})
        tunnels = frp.get("tunnels", [])
        serverinfo = frp.get("serverinfo") or {}
        ts_p = self.latest.get(constants.GROUP_TS_LATENCY, {})
        devices = ts_p.get("devices", [])
        sys_p = self.latest.get(constants.GROUP_SYSTEM, {})
        cert_p = self.latest.get(constants.GROUP_CERT_DAYS, {})
        certs = cert_p.get("certs", [])
        hs_p = self.latest.get(constants.GROUP_HEADSCALE, {})
        net_p = self.latest.get(constants.GROUP_NETWORK, {})
        probes = net_p.get("probes", [])
        grm_p = self.latest.get(constants.GROUP_GRM, {})
        lab_p = self.latest.get(constants.GROUP_LAB, {})
        updated = max(self.latest_ts.values(), default=None)
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
                "experiment_active": lab_p.get("experiment_active"),
                "experiments_total": lab_p.get("experiments_total"),
                "alerts_crit": lab_p.get("alerts_crit"),
                "alerts_warn": lab_p.get("alerts_warn"),
                "monitor_enabled": lab_p.get("monitor_enabled"),
                "experiment": lab_p.get("experiment"),
            },
            "alerts": {"unresolved": db.count_unresolved_alerts()},
            "nodes": [
                {"id": n["id"], "role": n.get("role"),
                 "last_seen_ts": n.get("last_seen_ts")}
                for n in db.get_nodes()
            ],
        }
