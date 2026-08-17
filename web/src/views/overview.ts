/** 总览视图：6 KPI（ASCII sparkline）+ 设备/隧道/证书三窗口。 */
import { sparkline } from "../ascii";
import { fmtBytes, fmtMs } from "../fmt";
import { S } from "../state";
import type { OverviewData } from "../types";
import { esc, pageHead, st, statusLevel, tb, win } from "../ui";

function kpi(
  idx: string,
  label: string,
  val: string,
  sm: string,
  s1: string,
  s2: string,
  extra: string,
  spCls: string,
  sparkKey: keyof typeof S.spark,
): string {
  const sp = S.spark[sparkKey];
  return '<div class="kpi ' + extra + '"><div class="t"><span><span class="idx">' + idx +
    "</span> " + label + "</span></div>" +
    '<div class="v">' + val + (sm ? "<small>" + esc(sm) + "</small>" : "") + "</div>" +
    '<div class="s"><span>' + esc(s1) + "</span><b>" + esc(s2) + "</b></div>" +
    (sp && sp.length ? '<div class="sp">' + sparkline(sp, spCls) + "</div>" : "") +
    "</div>";
}

export function renderOverview(): string {
  const o = S.overview as OverviewData | null;
  if (!o) {
    return pageHead("~/console", "总览", "连接中…", "monitor overview --live") +
      win("等待数据", "首次采集 ~2s", '<div class="empty">正在连接后端 API…</div>');
  }
  const f = o.frp, t = o.tailscale, s = o.system, c = o.certs, n = o.network, h = o.headscale;
  const certBad = (c.warn || 0) + (c.crit || 0);
  const expiring = c.expiring && c.expiring[0];
  const disk = s.disk && (s.disk["/"] || Object.values(s.disk)[0]);
  const net = s.net || {};
  const load = s.load && s.load.length ? s.load[0] : null;
  const probesOk = n.ok != null ? n.ok + "/" + n.total : "—";

  return pageHead("~/console", "总览", "LIVE 5s", "monitor overview --node cloud-1 --live") +
    '<div class="kpis">' +
    kpi("01", "CLOUD CPU",
      s.cpu_percent != null ? String(s.cpu_percent) : "—", s.cpu_percent != null ? "%" : "",
      "mem " + (s.mem_percent != null ? s.mem_percent + "%" : "—"),
      "load " + (load != null ? load : "—"), "", "", "cpu") +
    kpi("02", "CLOUD MEM",
      s.mem_percent != null ? String(s.mem_percent) : "—", s.mem_percent != null ? "%" : "",
      "disk / " + (disk && disk.percent != null ? disk.percent + "%" : "—"),
      "rx " + (net.rx_kbps != null ? net.rx_kbps + "k" : "—"), "", "", "mem") +
    kpi("03", "FRP TUNNELS",
      f.online != null ? f.online + "/" + f.total : "—", "在线",
      "in " + fmtBytes(f.traffic_in), "out " + fmtBytes(f.traffic_out),
      f.degraded ? "warn" : "", "amber", "frp") +
    kpi("04", "TAILNET",
      t.online != null ? t.online + "/" + t.total : "—", "在线",
      t.any_derp ? "DERP 中继" : "直连", "region " + (h.derp_region != null ? h.derp_region : "—"),
      "", "violet", "ts") +
    kpi("05", "CERTS", certBad ? String(certBad) : "0", certBad ? "异常" : "",
      expiring ? expiring.domain + " " + Math.round(expiring.days_left) + "d" : "全部正常",
      c.total != null ? c.total + " 张" : "—", certBad ? "warn" : "", "amber", "cpu") +
    kpi("06", "PROBES", probesOk, "可达",
      (n.fail || 0) + " fail", "共 " + (n.total || 0) + " 目标", "", "", "cpu") +
    "</div>" +
    '<div class="grid2">' +
    win("设备 · devices", "tailscale status --json · 30s",
      tb(["HOST", "IP", "OS", "状态", "路径", "延迟", "8899"],
        (S.devices ? S.devices.devices : []).map((d) =>
          "<tr><td>" + esc(d.name) + '</td><td class="muted">' + esc(d.tailscale_ip) +
          "</td><td>" + esc(d.os || "—") + "</td><td>" +
          st(statusLevel(d.status || (d.online ? "ok" : "off")), d.online ? "online" : "offline") +
          '</td><td class="dim2">' + (d.relay ? "DERP(" + esc(d.relay) + ")" : "直连") +
          '</td><td class="num">' + (d.timed_out ? '<span style="color:var(--warn)">超时</span>' : fmtMs(d.latency_ms)) +
          '</td><td class="muted">' + esc(d.mapped_8899 || "—") + "</td></tr>").join(""))) +
    win("隧道 · tunnels", "frps :7500 · 30s",
      tb(["NAME", "PORT", "状态", "IN", "OUT", "CONN"],
        (S.frp ? S.frp.tunnels : []).map((tn) =>
          "<tr><td>" + esc(tn.tunnel_id) + '</td><td class="num">' + esc(String(tn.remote_port ?? "—")) +
          "</td><td>" + st(tn.status === "online" ? "ok" : "crit", tn.status) +
          '</td><td class="num">' + fmtBytes(tn.traffic_in) + '</td><td class="num">' +
          fmtBytes(tn.traffic_out) + '</td><td class="num">' + esc(String(tn.cur_conns ?? "—")) + "</td></tr>").join(""))) +
    "</div>" +
    win("证书 · certs", "openssl -enddate · 1h",
      tb(["DOMAIN", "到期", "剩余", "状态"],
        (S.certs ? S.certs.certs : []).map((c2) =>
          "<tr><td>" + esc(c2.domain) + '</td><td class="muted">' + esc(c2.not_after || "—") +
          '</td><td class="num">' + Math.round(c2.days_left) + ' 天</td><td>' +
          st(statusLevel(c2.status), c2.status) + "</td></tr>").join("")));
}
