/** 轮询结果缓存：pollers 写入，视图读取；切换视图时立即刷新。 */
import type {
  AlertsData, CertsData, DevicesData, FrpData, HealthzData,
  NetworkData, OverviewData,
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
  alerts: AlertsData | null;
  healthz: HealthzData | null;
  spark: SparkCache;
  cur: string;
} = {
  overview: null,
  devices: null,
  frp: null,
  certs: null,
  probes: null,
  alerts: null,
  healthz: null,
  spark: { cpu: null, mem: null, frp: null, ts: null },
  cur: "overview",
};

export function setCur(v: string): void {
  S.cur = v;
}
