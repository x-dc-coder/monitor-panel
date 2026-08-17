/** 轮询结果缓存：pollers 写入，视图读取；切换视图时立即刷新。 */
import type {
  AlertsData, CertsData, DevicesData, FrpData, HealthzData,
  NetworkData, OverviewData, ServicesData,
} from "./types";

export interface SparkCache {
  cpu: number[] | null;
  mem: number[] | null;
  frp: number[] | null;
  ts: number[] | null;
}

export const S: {
  overview: OverviewData | null;
  devices: DevicesData | null;
  frp: FrpData | null;
  certs: CertsData | null;
  probes: NetworkData | null;
  services: ServicesData | null;
  alerts: AlertsData | null;
  healthz: HealthzData | null;
  spark: SparkCache;
  cur: string;
  /** 手动 ping 终端输出（全局，轮询重渲染不丢失） */
  pingLog: string | null;
} = {
  overview: null,
  devices: null,
  frp: null,
  certs: null,
  probes: null,
  services: null,
  alerts: null,
  healthz: null,
  spark: { cpu: null, mem: null, frp: null, ts: null },
  cur: "overview",
  pingLog: null,
};

export function setCur(v: string): void {
  S.cur = v;
}
