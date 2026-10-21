/** 设备视图：通用 DataTable（分页 + 状态/OS 筛选 + 按状态聚合）+ tailscale ping 终端输出交互。 */
import { fmtMs, nowTime } from "../fmt";
import { S } from "../state";
import { api } from "../api";
import type { DeviceRow, PingData } from "../types";
import { esc, pageHead, st, statusLevel, win } from "../ui";
import { table, type TableDef } from "../table";

const TABLE_ID = "devices";

function statusCell(d: DeviceRow): string {
  const lvl = statusLevel(d.status || (d.online ? "ok" : "off"));
  const txt = d.online ? (d.status === "degraded" ? "degraded" : "online") : "offline";
  return st(lvl, txt);
}

function latCell(d: DeviceRow): string {
  // 离线设备的 latency 是陈旧残留值，显示 "—" 而非误导性数字（2026-10-20 面板实录）
  if (!d.online) return '<span class="muted">—</span>';
  if (d.timed_out) return '<span style="color:var(--warn)">超时</span>';
  return '<span class="num">' + fmtMs(d.latency_ms) + "</span>";
}

function pingCell(d: DeviceRow): string {
  return '<button type="button" class="tbtn ping-btn" data-id="' + esc(d.device_id) +
    '" data-ip="' + esc(d.tailscale_ip || "") + '" data-n="' + esc(d.name || "") + '">[ping]</button>';
}

/** 路径徽章：直连 vs DERP（带区域名） */
function pathCell(d: DeviceRow): string {
  if (!d.online) return '<span class="muted">—</span>';
  if (d.relay) {
    return '<span class="badge dev-derp-badge" title="DERP 中继路径">DERP(' + esc(d.relay) + ")</span>";
  }
  return '<span class="badge dev-direct-badge" title="P2P 直连打洞">直连 Direct</span>';
}

/** 密钥到期（Key Expiry）倒计时与预警指示 */
function keyExpiryCell(d: DeviceRow): string {
  const days = d.key_days_left;
  if (days === null || days === undefined) {
    return '<span class="muted">—</span>';
  }
  if (days <= 0) {
    return '<span class="key-crit" title="密钥已过期，无法重连">已过期 ' + Math.abs(Math.round(days)) + "d</span>";
  }
  if (days < 3) {
    return '<span class="key-crit" title="严重警告：密钥即将到期">' + Math.round(days) + "d 🚨</span>";
  }
  if (days < 14) {
    return '<span class="key-warn" title="预警：请及时授权续期">' + Math.round(days) + "d ⚠️</span>";
  }
  return '<span class="num muted">' + Math.round(days) + "d</span>";
}

/** 节点能力徽章：出口节点（Exit Node）与子网路由（Subnet Router） */
function roleBadgesCell(d: DeviceRow): string {
  const badges: string[] = [];
  if (d.exit_node) {
    badges.push('<span class="badge dev-exit-badge" title="当前活跃的出口节点">Exit Node ★</span>');
  } else if (d.exit_node_option) {
    badges.push('<span class="badge dev-exit-opt" title="支持作为出口节点">Exit Opt</span>');
  }

  if (d.subnet_routes && d.subnet_routes.length > 0) {
    const routeTitle = d.subnet_routes.join(", ");
    badges.push('<span class="badge dev-subnet-badge" title="子网路由广播: ' + esc(routeTitle) + '">Subnet (' + d.subnet_routes.length + ")</span>");
  }

  return badges.length ? '<div class="dev-role-badges">' + badges.join(" ") + "</div>" : '<span class="muted">—</span>';
}

/** 主机与 MagicDNS 展示 */
function hostCell(d: DeviceRow): string {
  const hostName = esc(d.name || d.device_id);
  const dnsName = d.dns_name ? '<div class="muted sm dev-dns-name" title="MagicDNS 完整域名">' + esc(d.dns_name) + "</div>" : "";
  return '<div class="dev-host-cell"><b>' + hostName + "</b>" + dnsName + "</div>";
}

const def: TableDef = {
  cols: [
    { key: "name", label: "HOST / MagicDNS", fmt: hostCell },
    { key: "tailscale_ip", label: "IP", fmt: (d) => '<span class="muted num">' + esc(d.tailscale_ip || "—") + "</span>" },
    { key: "os", label: "OS", filterable: true, sec: true, fmt: (d) => esc(d.os || "—") },
    { key: "status", label: "状态", filterable: true, fmt: statusCell },
    { key: "path", label: "路径", filterable: true, fmt: pathCell },
    { key: "role", label: "路由 / 出口", sec: true, fmt: roleBadgesCell },
    { key: "key_expiry", label: "密钥到期", sec: true, fmt: keyExpiryCell },
    { key: "latency_ms", label: "延迟(中位)", fmt: latCell },
    { key: "mapped_8899", label: "8899", sec: true, fmt: (d) => '<span class="muted">' + esc(d.mapped_8899 || "—") + "</span>" },
    { key: "ping", label: "", fmt: pingCell },
  ],
  groupBy: "status",
  emptyMsg: "暂无设备 · 等待采集",
};

export function renderDevices(): string {
  const devs = S.devices ? S.devices.devices : [];
  const online = devs.filter((d) => d.online).length;
  const keyWarningCount = devs.filter((d) => d.key_days_left !== null && d.key_days_left !== undefined && d.key_days_left < 14).length;

  const summary = online + " 在线" +
    (keyWarningCount > 0 ? ' · <span style="color:var(--warn)">' + keyWarningCount + " 密钥预警</span>" : "");

  return pageHead("~/console/devices", "设备", summary,
    "tailscale status --json && tailscale ping <ip> --c 3 · MagicDNS / 密钥到期 / DERP / 子网路由") +
    win("设备清单 · devices", devs.length + " 台 · ping 轮巡 60s/台 · Semaphore(2)",
      table(def, devs, TABLE_ID)) +
    (S.pingLog ? '<div class="tout" id="ping-out">' + S.pingLog + '</div>'
      : '<div class="tout" id="ping-out"><div class="ln"><span class="ps">$</span> ' +
        '<span class="out">tailscale ping &lt;ip&gt; — 点击 [ping] 执行<span class="caret"></span></span></div></div>');
}

/** 绑定 [ping] 按钮（视图渲染后调用）；429 显示限流提示 */
export function bindPing(): void {
  document.querySelectorAll<HTMLButtonElement>(".ping-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const id = btn.dataset.id || "";
      const ip = btn.dataset.ip || "";
      const n = btn.dataset.n || "";
      const out = document.getElementById("ping-out");
      if (!out) return;
      btn.disabled = true;
      btn.textContent = "…";
      out.classList.add("show");
      const cmdLine = '<div class="ln"><span class="muted">' + nowTime() +
        '</span> <span class="ps">$</span> <span class="out">sudo tailscale ping ' + esc(ip) + "</span></div>";
      S.pingLog = cmdLine;
      out.innerHTML = cmdLine;
      api<PingData>("/api/devices/" + encodeURIComponent(id) + "/ping", { method: "POST" })
        .then((d) => {
          const okLine = '<div class="ln"><span class="ok2">pong from ' + esc(n) + " (" + esc(ip) +
            ") via " + esc(d.path === "direct" ? "direct" : "DERP(headscale)") + " in " +
            fmtMs(d.latency_ms) + " ✓</span></div>" +
            '<div class="ln"><span class="out">loss ' + (d.loss_percent || 0) + "% · " +
            (d.mock ? "mock 数据" : "实时采集") + "</span></div>";
          out.innerHTML += okLine;
          S.pingLog = (S.pingLog || "") + okLine;
          btn.textContent = fmtMs(d.latency_ms);
        })
        .catch((e: Error) => {
          const errLine = '<div class="ln"><span class="err2">✕ ' + esc(e.message) +
            "（限流 5s/设备）</span></div>";
          out.innerHTML += errLine;
          S.pingLog = (S.pingLog || "") + errLine;
          btn.textContent = "限流";
        })
        .finally(() => {
          setTimeout(() => { btn.textContent = "[ping]"; btn.disabled = false; }, 5000);
        });
    });
  });
}
