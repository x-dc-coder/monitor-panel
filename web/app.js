/* monitor.dc-sy.cn 统一监控面板 - 轮询/渲染/图表/阈值配色 */
"use strict";

/* ---------------- 工具 ---------------- */
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function fmtBytes(v) {
  if (v == null || isNaN(v)) return "-";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return v.toFixed(i ? 1 : 0) + " " + u[i];
}
function fmtMs(v) {
  if (v == null || isNaN(v)) return "-";
  return v >= 100 ? Math.round(v) + " ms" : v.toFixed(1) + " ms";
}
function fmtPct(v) { return v == null || isNaN(v) ? "-" : v.toFixed(1) + "%"; }
function fmtTs(ts) {
  if (!ts) return "-";
  const d = new Date(ts * 1000);
  const p = (n) => String(n).padStart(2, "0");
  return p(d.getHours()) + ":" + p(d.getMinutes()) + ":" + p(d.getSeconds());
}
function fmtDate(ts) {
  if (!ts) return "-";
  const d = new Date(ts * 1000);
  return d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" +
    String(d.getDate()).padStart(2, "0");
}
function setDot(id, state) {
  const el = $(id);
  if (el) el.className = "dot " + (state || "unknown");
}
function pill(cls, text) { return '<span class="pill ' + cls + '">' + esc(text) + "</span>"; }

async function api(path, opts) {
  const r = await fetch(path, opts);
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.detail || j.error || ("HTTP " + r.status));
  return j;
}

/* ---------------- 总览卡片 ---------------- */
function renderCards(o) {
  const cards = [];
  const sys = o.system || {};
  const disk = sys.disk || {};
  const rootDisk = disk["/"] || Object.values(disk)[0] || {};
  const dp = rootDisk.percent;
  const diskCls = dp >= 92 ? "crit" : dp >= 85 ? "warn" : "";
  const cpuP = sys.cpu_percent, memP = sys.mem_percent;
  const cpuCls = cpuP >= 92 ? "crit" : cpuP >= 85 ? "warn" : "";
  const memCls = memP >= 92 ? "crit" : memP >= 85 ? "warn" : "";
  const f = o.frp || {};
  const t = o.tailscale || {};
  const c = o.certs || {};
  const n = o.network || {};
  const a = (o.alerts || {}).unresolved || 0;
  const hs = o.headscale || {};
  const bar = (pct, cls) =>
    '<div class="bar"><i class="' + (cls || "") + '" style="width:' +
    Math.min(100, pct || 0) + '%"></i></div>';

  cards.push({ h: "云主机健康", big: (cpuP == null ? "-" : cpuP + "%"), bigS: "CPU",
    body: '<div class="row"><span>内存</span><b>' + fmtPct(memP) + "</b></div>" +
      bar(memP, memCls) +
      '<div class="row"><span>磁盘 /</span><b>' + fmtPct(dp) + "</b></div>" +
      bar(dp, diskCls) +
      '<div class="sub">负载 ' + esc((sys.load || []).join(" / ")) + "</div>" });
  cards.push({ h: "frp 隧道", big: (f.online || 0) + "/" + (f.total || 0), bigS: "在线",
    body: '<div class="row"><span>总入</span><b>' + fmtBytes(f.traffic_in) + "</b></div>" +
      '<div class="row"><span>总出</span><b>' + fmtBytes(f.traffic_out) + "</b></div>" +
      (f.degraded ? '<div class="sub" style="color:var(--warn)">dashboard 降级</div>'
        : '<div class="sub">' + esc(f.proxy_mode || "per-type") + "</div>") });
  cards.push({ h: "Tailscale 设备", big: (t.online || 0) + "/" + (t.total || 0), bigS: "在线",
    body: '<div class="row"><span>中继</span><b>' + (t.any_derp ? "DERP" : "直连") + "</b></div>" +
      '<div class="row"><span>控制面</span><b>' + (hs.tcp_ok ? "OK" : "DOWN") + "</b></div>" +
      '<div class="sub">节点 ' + (hs.nodes_total == null ? "-" : hs.nodes_total) +
      " · region " + (hs.derp_region || 999) + "</div>" });
  cards.push({ h: "SSL 证书", big: (c.total || 0) + "", bigS: "张",
    body: '<div class="row"><span>警告</span><b style="color:var(--warn)">' + (c.warn || 0) +
      "</b></div>" +
      '<div class="row"><span>严重</span><b style="color:var(--crit)">' + (c.crit || 0) +
      "</b></div>" +
      '<div class="sub">最近到期 ' + (c.expiring && c.expiring[0]
        ? esc(c.expiring[0].domain) + " " + (c.expiring[0].days_left | 0) + "d"
        : "-") + "</div>" });
  cards.push({ h: "告警", big: a + "", bigS: "未解决",
    body: '<div class="row"><span>冷却</span><b>30 min</b></div>' +
      '<div class="row"><span>保留</span><b>90 天</b></div>' +
      '<div class="sub">' + (a ? "需要关注" : "一切正常") + "</div>" });
  cards.push({ h: "网络探测", big: (n.ok || 0) + "/" + (n.total || 0), bigS: "可达",
    body: '<div class="row"><span>失败</span><b style="color:var(--crit)">' + (n.fail || 0) +
      "</b></div>" +
      '<div class="sub">多目标公网连通</div>' });

  $("cards").innerHTML = cards.map((c) =>
    '<div class="card"><h3>' + c.h + '</h3><div class="big">' + c.big +
    " <small>" + c.bigS + "</small></div>" + c.body + "</div>").join("");

  /* 头部状态点 */
  setDot("dot-frps", f.degraded ? "warn" : "ok");
  $("hdr-frps").textContent = f.degraded ? "降级" : (f.total || 0) + " 隧道";
  setDot("dot-hs", hs.tcp_ok ? "ok" : "crit");
  $("hdr-hs").textContent = hs.tcp_ok ? "OK" : "DOWN";
  setDot("dot-legacy", f.total > 0 && !f.degraded ? "ok" : "unknown");
  $("hdr-legacy").textContent = f.total ? "已接入" : "-";
  $("hdr-updated").textContent = fmtTs(o.updated_at) + " (" + fmtDate(o.updated_at) + ")";
  $("hdr-mock").textContent = o.mock ? "MOCK 模式" : "真实数据";
  $("hdr-mock").className = "badge" + (o.mock ? " mock" : "");
  const nodes = (o.nodes || []).map((x) => x.id).join(", ");
  $("hdr-node").textContent = "节点 " + (nodes || "-");
}

/* ---------------- 设备表 ---------------- */
function renderDevices(data) {
  const rows = (data.devices || []).map((d) => {
    const st = d.status || (d.online ? "online" : "offline");
    const lat = d.timed_out ? '<span style="color:var(--warn)">超时</span>' : fmtMs(d.latency_ms);
    const pingBtn = d.online
      ? '<button class="ping-btn" data-device="' + esc(d.device_id) + '">ping</button>'
      : "";
    return "<tr><td>" + esc(d.name) + "</td>" +
      '<td class="mono">' + esc(d.tailscale_ip) + "</td>" +
      "<td>" + esc(d.os || "-") + "</td>" +
      "<td>" + pill(st, st) + "</td>" +
      '<td class="mono">' + (d.relay ? "DERP(" + esc(d.relay) + ")" : "直连") + "</td>" +
      '<td class="mono">' + lat + "</td>" +
      '<td class="mono">' + fmtPct(d.loss_percent) + "</td>" +
      '<td class="mono">' + (d.mapped_8899 ? esc(d.mapped_8899) : "-") + "</td>" +
      "<td>" + pingBtn + "</td></tr>";
  }).join("");
  $("tbl-devices").querySelector("tbody").innerHTML =
    rows || '<tr><td colspan="9" style="color:var(--text-dim)">暂无数据（等待采集）</td></tr>';
}

/* ---------------- frp 隧道表 ---------------- */
function renderTunnels(frp) {
  const rows = (frp.tunnels || []).map((t) =>
    "<tr><td>" + esc(t.tunnel_id) + "</td>" +
    '<td class="mono">' + esc(t.type || "-") + "</td>" +
    '<td class="mono">' + esc(t.remote_port == null ? "-" : t.remote_port) + "</td>" +
    '<td class="mono">' + esc(t.local_addr || "-") + "</td>" +
    "<td>" + pill(t.status === "online" ? "online" : "offline", t.status || "?") + "</td>" +
    '<td class="mono">' + fmtBytes(t.traffic_in) + "</td>" +
    '<td class="mono">' + fmtBytes(t.traffic_out) + "</td>" +
    '<td class="mono">' + esc(t.cur_conns == null ? "-" : t.cur_conns) + "</td></tr>"
  ).join("");
  $("tbl-tunnels").querySelector("tbody").innerHTML =
    rows || '<tr><td colspan="8" style="color:var(--text-dim)">暂无隧道（dashboard 不可用或未采集）</td></tr>';
}

/* ---------------- frp 链路质量（8899） ---------------- */
function renderLink(frp) {
  const rows = (frp.link || []).map((l) => {
    const alive = (v) => v == null ? "-" : (v ? pill("online", "在线") : pill("crit", "DOWN"));
    return "<tr><td>" + esc(l.device_id) + "</td>" +
      '<td class="mono">' + esc(l.hostname || "-") + "</td>" +
      '<td class="mono">' + fmtMs(l.latency_ms) + "</td>" +
      '<td class="mono">' + fmtPct(l.packet_loss_percent) + "</td>" +
      "<td>" + alive(l.frpc_alive) + "</td><td>" + alive(l.frps_alive) + "</td>" +
      '<td class="mono">' + (l.bandwidth_up_kbps == null ? "-" : l.bandwidth_up_kbps + " kb/s") + "</td>" +
      '<td class="mono">' + (l.bandwidth_down_kbps == null ? "-" : l.bandwidth_down_kbps + " kb/s") + "</td></tr>";
  }).join("");
  $("tbl-link").querySelector("tbody").innerHTML =
    rows || '<tr><td colspan="8" style="color:var(--text-dim)">暂无 8899 agent 数据（未配置 token 或 agent 离线）</td></tr>';
}

/* ---------------- 证书 / 探测 ---------------- */
function renderCerts(data) {
  const rows = (data.certs || []).map((c) =>
    "<tr><td>" + esc(c.domain) + "</td>" +
    '<td class="mono">' + esc(c.not_after || "-") + "</td>" +
    '<td class="mono">' + (c.days_left < 0 ? Math.round(c.days_left) + "d (已过期)" :
      Math.round(c.days_left) + " 天") + "</td>" +
    "<td>" + pill(c.status, c.status) + "</td></tr>").join("");
  $("tbl-certs").querySelector("tbody").innerHTML =
    rows || '<tr><td colspan="4" style="color:var(--text-dim)">暂无证书数据</td></tr>';
}
function renderNetwork(data) {
  const rows = (data.probes || []).map((p) =>
    "<tr><td>" + esc(p.name) + "</td>" +
    '<td class="mono">' + esc(p.kind) + "</td>" +
    '<td class="mono">' + esc(p.target) + (p.port ? ":" + esc(p.port) : "") + "</td>" +
    '<td class="mono">' + fmtMs(p.latency_ms) + "</td>" +
    "<td>" + (p.reachable ? pill("ok", "可达") : pill("crit", "不可达")) + "</td></tr>").join("");
  $("tbl-network").querySelector("tbody").innerHTML =
    rows || '<tr><td colspan="5" style="color:var(--text-dim)">暂无探测数据</td></tr>';
}

/* ---------------- 告警流 ---------------- */
function renderAlerts(data) {
  const items = (data.alerts || []).map((al) =>
    '<li class="' + (al.resolved ? "resolved" : "") + '">' +
    '<span class="ts">' + fmtTs(al.ts) + "</span>" +
    '<span class="lvl ' + esc(al.level) + '">' + esc(al.level.toUpperCase()) + "</span>" +
    '<span class="msg">' + esc(al.message) + ' <small style="color:var(--text-dim)">[' +
    esc(al.source) + "]</small></span></li>").join("");
  $("lst-alerts").innerHTML = items || '<li style="color:var(--text-dim)">暂无告警</li>';
}

/* ---------------- 趋势图 ---------------- */
let chart = null;
const STATIC_METRICS = [
  ["system|cpu_percent", "云主机 · CPU %"],
  ["system|mem_percent", "云主机 · 内存 %"],
  ["system|disk_percent", "云主机 · 磁盘 %"],
  ["system|load_1m", "云主机 · 负载 1m"],
  ["system|net_rx_kbps", "云主机 · 下行 kb/s"],
  ["system|net_tx_kbps", "云主机 · 上行 kb/s"],
  ["frp_traffic|total_traffic_in", "frp · 总入流量"],
  ["frp_traffic|total_traffic_out", "frp · 总出流量"],
  ["frp_link|latency_ms.desktop", "frp 链路 · desktop 延迟"],
  ["frp_link|latency_ms.laptop", "frp 链路 · laptop 延迟"],
  ["frp_link|latency_ms.cloud-server", "frp 链路 · cloud-server 延迟"],
  ["headscale|nodes_total", "headscale · 节点数"],
  ["headscale|queue_depth", "headscale · 队列积压"],
];
const BYTE_METRICS = new Set(["total_traffic_in", "total_traffic_out"]);

function rebuildMetricOptions() {
  const sel = $("sel-metric");
  const prev = sel.value;
  const opts = STATIC_METRICS.map(([v, l]) => '<option value="' + v + '">' + l + "</option>");
  /* 动态追加 tailscale 设备延迟 */
  document.querySelectorAll("#tbl-devices tbody tr").forEach((tr) => {
    const name = tr.children[0] ? tr.children[0].textContent : "";
    if (name) opts.push('<option value="ts_latency|latency_ms.' + esc(name) + '">' +
      "ts 设备 · " + esc(name) + " 延迟</option>");
  });
  sel.innerHTML = opts.join("");
  if (prev && Array.from(sel.options).some((o) => o.value === prev)) sel.value = prev;
}

function initChart() {
  const ctx = $("chart").getContext("2d");
  chart = new Chart(ctx, {
    type: "line",
    data: { labels: [], datasets: [{ label: "", data: [], borderColor: "#38bdf8",
      backgroundColor: "rgba(56,189,248,.12)", fill: true, tension: .25,
      pointRadius: 1.5, borderWidth: 1.6 }] },
    options: {
      responsive: true, maintainAspectRatio: false, animation: false,
      plugins: { legend: { display: false },
        tooltip: { callbacks: { label: (c) => " " + c.parsed.y } } },
      scales: {
        x: { ticks: { color: "#8ba0b2", maxTicksLimit: 10, maxRotation: 0 },
          grid: { color: "rgba(38,49,61,.4)" } },
        y: { ticks: { color: "#8ba0b2", callback: (v) => chartTicks(v) },
          grid: { color: "rgba(38,49,61,.4)" } },
      },
    },
  });
}
function chartTicks(v) {
  const m = $("sel-metric").value.split("|")[1] || "";
  if (BYTE_METRICS.has(m.split(".")[0])) return fmtBytes(v);
  if (m.startsWith("net_")) return v + "k";
  return v;
}

async function loadHistory() {
  const [group, metric] = ($("sel-metric").value || "system|cpu_percent").split("|");
  const range = $("sel-range").value;
  $("chart-hint").textContent = "加载中…";
  try {
    const j = await api("/api/history?metric=" + encodeURIComponent(metric) +
      "&group=" + encodeURIComponent(group) + "&range=" + range);
    const pts = j.data.points || [];
    chart.data.labels = pts.map((p) => fmtTs(p.ts));
    chart.data.datasets[0].data = pts.map((p) => p.value);
    chart.data.datasets[0].label = metric + "（桶 " + j.data.bucket_s + "s）";
    chart.update();
    $("chart-hint").textContent = j.data.count + " 点 · 桶 " + j.data.bucket_s + "s · 降采样 AVG";
  } catch (e) {
    $("chart-hint").textContent = "加载失败: " + e.message;
  }
}

/* ---------------- 手动 ping ---------------- */
async function doPing(deviceId, btn) {
  btn.disabled = true;
  const old = btn.textContent;
  btn.textContent = "…";
  try {
    const j = await api("/api/devices/" + encodeURIComponent(deviceId) + "/ping",
      { method: "POST" });
    btn.textContent = fmtMs(j.data.latency_ms);
    setTimeout(() => { btn.textContent = "ping"; btn.disabled = false; }, 5000);
  } catch (e) {
    btn.textContent = "限流";
    setTimeout(() => { btn.textContent = "ping"; btn.disabled = false; }, 5000);
  }
}

/* ---------------- 轮询 ---------------- */
async function pollOverview() {
  try {
    const j = await api("/api/overview");
    renderCards(j.data);
    const a = await api("/api/alerts?limit=30");
    renderAlerts(a.data);
  } catch (e) { /* 网络暂断，静默 */ }
}
async function pollDevices() {
  try {
    const j = await api("/api/devices");
    renderDevices(j.data);
    rebuildMetricOptions();
  } catch (e) { /* 静默 */ }
}
async function pollFrp() {
  try {
    const j = await api("/api/frp");
    renderTunnels(j.data);
    renderLink(j.data);
  } catch (e) { /* 静默 */ }
}
async function pollCerts() {
  try {
    const j = await api("/api/certs");
    renderCerts(j.data);
  } catch (e) { /* 静默 */ }
}
async function pollNetwork() {
  try {
    const j = await api("/api/network");
    renderNetwork(j.data);
  } catch (e) { /* 静默 */ }
}

/* ---------------- 事件绑定 ---------------- */
document.addEventListener("click", (e) => {
  const btn = e.target.closest(".ping-btn");
  if (btn) doPing(btn.dataset.device, btn);
});
$("sel-metric").addEventListener("change", loadHistory);
$("sel-range").addEventListener("change", loadHistory);
$("btn-ping-all").addEventListener("click", () => {
  document.querySelectorAll(".ping-btn:not(:disabled)").forEach((b) => doPing(b.dataset.device, b));
});

/* ---------------- 启动 ---------------- */
(function boot() {
  initChart();
  pollOverview(); pollDevices(); pollFrp(); pollCerts(); pollNetwork();
  setInterval(pollOverview, 5000);
  setInterval(pollDevices, 10000);
  setInterval(pollFrp, 10000);
  setInterval(pollCerts, 30000);
  setInterval(pollNetwork, 30000);
  setInterval(loadHistory, 30000);
})();
