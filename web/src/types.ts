/**
 * API 契约类型 —— 与后端 app/api/* 的 {ok, data, ts} 信封严格对齐。
 * 后端任一字段漂移都会在 tsc --noEmit 时暴露。
 */

/** 统一响应信封 */
export interface Envelope<T> {
  ok: boolean;
  data: T;
  ts: number;
  error?: string;
}

/** 状态级别（前端语义色） */
export type StatusLevel = "ok" | "warn" | "crit" | "off";

/* ---------------- /api/overview ---------------- */
export interface FrpSummary {
  total: number;
  online: number;
  traffic_in: number | null;
  traffic_out: number | null;
  degraded: boolean;
  proxy_mode: string | null;
  error: string | null;
}
export interface TailscaleSummary {
  total: number;
  online: number;
  backend_state: string | null;
  any_derp: boolean;
}
export interface SystemSummary {
  cpu_percent: number | null;
  mem_percent: number | null;
  load: number[] | null;
  net: { rx_kbps?: number; tx_kbps?: number } | null;
  disk: Record<string, { percent: number; total_gb: number; used_gb: number }> | null;
}
export interface HeadscaleSummary {
  tcp_ok: number | null;
  nodes_total: number | null;
  derp_region: number | null;
}
export interface CertSummaryItem {
  domain: string;
  days_left: number;
  not_after?: string;
  status: string;
}
export interface CertsSummary {
  total: number;
  warn: number;
  crit: number;
  expiring: CertSummaryItem[];
}
export interface NetworkSummary {
  total: number;
  ok: number;
  fail: number;
}
export interface GrmSummary {
  total: number;
  with_release: number;
  new_release: number;
  degraded: boolean;
  error: string | null;
  base_url: string | null;
}
export interface LabSummary {
  gpu_util_percent: number | null;
  vram_used_mib: number | null;
  vram_total_mib: number | null;
  vram_util_percent: number | null;
  gpu_temp_c: number | null;
  cpu_percent: number | null;
  mem_percent: number | null;
  mem_used_mib: number | null;
  mem_total_mib: number | null;
  experiment_active: number | null;
  experiments_total: number | null;
  alerts_crit: number | null;
  alerts_warn: number | null;
  monitor_enabled: number | null;
  experiment: {
    runId?: string;
    cmd?: string;
    type?: string;
    startTs?: number;
  } | null;
  procs_top?: {
    pid: number;
    ppid: number | null;
    cmd: string;
    cpuPct: number | null;
    memMiB: number | null;
  }[];
  tags?: {
    label: string;
    kind: string;
    pids: number[];
    gpuUtilPct: number | null;
    cpuPct: number | null;
    memMiB: number | null;
  }[];
}
export interface NodeInfo {
  id: string;
  role?: string;
  last_seen_ts?: number | null;
}
export interface OverviewData {
  mock: boolean;
  mode?: "mock" | "snapshot" | "live";
  updated_at: number | null;
  frp: FrpSummary;
  tailscale: TailscaleSummary;
  system: SystemSummary;
  headscale: HeadscaleSummary;
  certs: CertsSummary;
  network: NetworkSummary;
  grm?: GrmSummary;
  lab?: LabSummary;
  alerts: { unresolved: number };
  nodes: NodeInfo[];
}

/* ---------------- /api/devices ---------------- */
export interface DeviceRow {
  device_id: string;
  name: string | null;
  tailscale_ip: string | null;
  os: string | null;
  online: boolean;
  status: string;
  relay: string | null;
  last_seen_ts: number | null;
  rx_bytes: number | null;
  tx_bytes: number | null;
  updated_ts: number | null;
  latency_ms: number | null;
  latency_min_ms: number | null;
  path: string | null;
  loss_percent: number | null;
  timed_out: boolean;
  ping_ts: number | null;
  mapped_8899: string | null;
}
export interface DevicesData {
  devices: DeviceRow[];
  device_map: Record<string, string>;
}
export interface PingResult {
  device_id: string;
  tailscale_ip?: string;
  latency_ms: number | null;
  latency_min_ms?: number;
  path: string;
  loss_percent: number;
  timed_out: boolean;
  mock?: boolean;
  ts?: number;
  error?: string;
}

/* ---------------- /api/frp ---------------- */
export interface TunnelRow {
  tunnel_id: string;
  type: string | null;
  remote_port: number | null;
  local_addr: string | null;
  status: string;
  traffic_in: number | null;
  traffic_out: number | null;
  cur_conns: number | null;
  last_start_time?: string | null;
}
export interface LinkRow {
  device_id: string;
  hostname: string | null;
  latency_ms: number | null;
  packet_loss_percent: number | null;
  frpc_alive: number | null;
  frps_alive: number | null;
  bandwidth_up_kbps: number | null;
  bandwidth_down_kbps: number | null;
  age_seconds?: number | null;
  display_status?: string | null;
}
export interface FrpData {
  tunnels: TunnelRow[];
  serverinfo: Record<string, unknown> | null;
  link: LinkRow[];
  degraded: boolean;
  proxy_mode: string | null;
  source: string | null;
  ts: number | null;
}

/* ---------------- /api/certs ---------------- */
export interface CertsData {
  certs: CertSummaryItem[];
  ts: number | null;
}

/* ---------------- /api/network ---------------- */
export interface ProbeRow {
  name: string;
  kind: string;
  target: string;
  port?: number | null;
  latency_ms: number | null;
  loss_percent?: number | null;
  reachable: boolean;
  error?: string | null;
  status_code?: number;
  ts?: number | null;
  mock?: boolean;
}
export interface NetworkData {
  probes: ProbeRow[];
  ts: number | null;
}

/* ---------------- /api/services ----------------
 * 类型来源：types.gen.ts（npm run gen:types，issue #6）——后端 app/api/schemas.py
 * 是唯一权威，API 加字段漏改这里 → tsc 编译期报错。 */
import type { components } from "./types.gen";

export type ServiceRow = components["schemas"]["ServiceItem"];
export type ServicesData = components["schemas"]["ServicesData"];

/* ---------------- /api/grm（issue #4：Release 监控只读聚合） ----------------
 * 类型来源：types.gen.ts（同 services，后端 schemas.py 权威） */
export type GrmRelease = components["schemas"]["GrmLatest"];
export type GrmRepoRow = components["schemas"]["GrmRepo"];
export type GrmData = components["schemas"]["GrmApiData"];

/* ---------------- /api/alerts ---------------- */
export type AlertLevel = "info" | "warn" | "crit";
export interface AlertRow {
  id: number;
  ts: number;
  level: AlertLevel;
  source: string;
  message: string;
  resolved: number;
}
export interface AlertsData {
  alerts: AlertRow[];
}

/* ---------------- /api/history ---------------- */
export interface HistoryPoint {
  ts: number;
  value: number;
  ts_max: number;
}
export interface HistoryData {
  metric: string;
  group: string | null;
  range: string;
  bucket_s: number;
  points: HistoryPoint[];
  count: number;
  since_ts: number;
}

/* ---------------- /api/healthz ---------------- */
export interface CollectorStat {
  interval: number;
  runs: number;
  skipped: number;
  errors: number;
  last_error: string | null;
  last_run_dur_s: number;
}
export interface HealthzData {
  ok: boolean;
  uptime: number;
  version: string;
  mock: boolean;
  node: string;
  collectors: Record<string, CollectorStat>;
}
