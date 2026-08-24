/**
 * 入口：hash 路由 + 轮询调度 + 视图装配（A1 精工终端）。
 * 轮询节奏：overview 5s / devices+frp+alerts 10s / certs+probes+healthz 30s / spark 30s
 */
import { api } from "./api";
import { S, setCur } from "./state";
import type {
  AlertsData, CertsData, DevicesData, FrpData, GrmData, HealthzData,
  HistoryData, NetworkData, OverviewData, ServicesData,
} from "./types";
import { fmtDate, fmtTs } from "./fmt";
import { isViewId, placeholder, views } from "./views";

/* ---------------- 顶栏 / 侧栏 ---------------- */
function renderStatusbar(): void {
  const o = S.overview;
  if (!o) return;
  const f = o.frp, hs = o.headscale, c = o.certs, t = o.tailscale;
  const exp = c.expiring && c.expiring[0];
  const set = (id: string, cls: string, txt: string): void => {
    const el = document.getElementById(id);
    if (el) { el.className = "sq " + cls; el.textContent = txt; }
  };
  set("sb-frps-dot", f.degraded ? "warn" : f.total > 0 ? "ok" : "off", "");
  document.getElementById("sb-frps")!.textContent = f.degraded ? "降级" : f.total + " 隧道";
  set("sb-hs-dot", hs.tcp_ok ? "ok" : "crit", "");
  document.getElementById("sb-hs")!.textContent = hs.tcp_ok ? "OK" : "DOWN";
  set("sb-cert-dot", (c.crit || 0) > 0 ? "crit" : (c.warn || 0) > 0 ? "warn" : "ok", "");
  document.getElementById("sb-cert")!.textContent = exp ? Math.round(exp.days_left) + "d" : "—";
  set("sb-ts-dot", t.total > 0 ? (t.online === t.total ? "ok" : "warn") : "off", "");
  document.getElementById("sb-ts")!.textContent = t.online + "/" + t.total;
  document.getElementById("sb-sync")!.textContent = fmtTs(o.updated_at);
  document.getElementById("sb-date")!.textContent = fmtDate(o.updated_at);
  const dot = (name: string, cls: string): void => {
    const el = document.querySelector('[data-st="' + name + '"]');
    if (el) el.className = "st " + cls;
  };
  dot("devices", t.total > 0 ? (t.online === t.total ? "ok" : "warn") : "off");
  dot("certs", (c.crit || 0) > 0 ? "crit" : (c.warn || 0) > 0 ? "warn" : "ok");
  dot("alerts", (o.alerts && o.alerts.unresolved) > 0 ? "crit" : "ok");
  const lab = o.lab;
  dot("lab", !lab ? "off" : lab.monitor_enabled === 1 ? "ok" : "warn");
  document.getElementById("foot-node")!.textContent =
    (o.nodes && o.nodes[0] && o.nodes[0].id) || "—";
  const modeTxt = o.mode === "snapshot" ? "SNAPSHOT" : o.mock ? "MOCK" : "实时";
  document.getElementById("foot-mode")!.textContent = modeTxt;
  document.getElementById("foot-mode")!.style.color =
    o.mode === "snapshot" ? "var(--violet)" : o.mock ? "var(--warn)" : "var(--ok)";
}

/* ---------------- 轮询 ---------------- */
async function pollOverview(): Promise<void> {
  try {
    S.overview = await api<OverviewData>("/api/overview");
    renderStatusbar();
    // overview 与 lab 视图共用 overview 数据，两者都需在轮询时重渲染
    if (S.cur === "overview" || S.cur === "lab") render();
  } catch { /* 网络暂断静默（保留旧数据） */ }
}
async function pollDevices(): Promise<void> {
  try {
    S.devices = await api<DevicesData>("/api/devices");
    if (S.cur === "devices" || S.cur === "overview") render();
  } catch { /* 静默 */ }
}
async function pollFrp(): Promise<void> {
  try {
    S.frp = await api<FrpData>("/api/frp");
    if (S.cur === "tunnels" || S.cur === "links" || S.cur === "overview") render();
  } catch { /* 静默 */ }
}
async function pollCerts(): Promise<void> {
  try {
    S.certs = await api<CertsData>("/api/certs");
    if (S.cur === "certs" || S.cur === "overview") render();
  } catch { /* 静默 */ }
}
async function pollProbes(): Promise<void> {
  try {
    S.probes = await api<NetworkData>("/api/network");
    if (S.cur === "probes") render();
  } catch { /* 静默 */ }
}
async function pollServices(): Promise<void> {
  try {
    S.services = await api<ServicesData>("/api/services");
    if (S.cur === "services") render();
  } catch { /* 静默 */ }
}
async function pollReleases(): Promise<void> {
  try {
    S.releases = await api<GrmData>("/api/grm");
    if (S.cur === "releases") render();
  } catch { /* 静默（grm 降级由 summary.degraded 表达） */ }
}
async function pollAlerts(): Promise<void> {
  try {
    S.alerts = await api<AlertsData>("/api/alerts?limit=50");
    if (S.cur === "alerts") render();
  } catch { /* 静默 */ }
}
async function pollHealthz(): Promise<void> {
  try {
    S.healthz = await api<HealthzData>("/api/healthz");
    if (S.cur === "settings") render();
  } catch { /* 静默 */ }
}

const SPARK_METRICS: Record<keyof typeof S.spark, [string, string]> = {
  cpu: ["cpu_percent", "system"],
  mem: ["mem_percent", "system"],
  frp: ["total_traffic_in", "frp_traffic"],
  ts: ["nodes_total", "headscale"],
  lab: ["gpu_util_percent", "lab_monitor"],
};

/** overview KPI 的 sparkline 数据（24h 降采样最后 9 点，30s 刷新一次） */
async function refreshSpark(): Promise<void> {
  const tasks = (Object.keys(SPARK_METRICS) as (keyof typeof S.spark)[]).map(async (k) => {
    const [m, g] = SPARK_METRICS[k];
    try {
      const j = await api<HistoryData>("/api/history?metric=" + encodeURIComponent(m) +
        "&group=" + encodeURIComponent(g) + "&range=24h");
      S.spark[k] = j.points.slice(-9).map((p) => p.value);
    } catch {
      S.spark[k] = null;
    }
  });
  await Promise.all(tasks);
}

/* ---------------- 路由与装配 ---------------- */
function bindView(v: string): void {
  if (v === "devices") void import("./views/devices").then((m) => m.bindPing());
  if (v === "links") void import("./views/links").then((m) => m.bindLinks());
  if (v === "probes") void import("./views/probes").then((m) => m.renderProbeOut());
  if (v === "lab") void import("./views/lab").then((m) => m.bindLab());
  // 通用表格交互（分页/筛选/分组）——各视图渲染后挂载
  void import("./table").then((m) => m.bindTableForView(v));
}

function render(): void {
  const v = location.hash.replace("#/", "") || "overview";
  setCur(v);
  document.querySelectorAll("#nav-tree a").forEach((a) =>
    a.classList.toggle("on", a.getAttribute("data-v") === v));
  const el = document.getElementById("view")!;
  el.innerHTML = isViewId(v) ? views[v]() : placeholder(v);
  bindView(v);
  el.scrollTop = 0;
}

// 表格内交互（筛选/分页/分组）触发重渲染
document.addEventListener("table-change", () => render());

window.addEventListener("hashchange", render);

function boot(): void {
  render();
  void refreshSpark();
  void pollOverview();
  void pollDevices();
  void pollFrp();
  void pollCerts();
  void pollProbes();
  void pollServices();
  void pollReleases();
  void pollAlerts();
  void pollHealthz();
  window.setInterval(() => void pollOverview(), 5000);
  window.setInterval(() => void pollDevices(), 10000);
  window.setInterval(() => void pollFrp(), 10000);
  window.setInterval(() => void pollAlerts(), 10000);
  window.setInterval(() => void pollCerts(), 30000);
  window.setInterval(() => void pollProbes(), 30000);
  window.setInterval(() => void pollServices(), 30000);
  window.setInterval(() => void pollReleases(), 30000);
  window.setInterval(() => void pollHealthz(), 30000);
  window.setInterval(() => void refreshSpark(), 30000);
}

boot();
