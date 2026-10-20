/** 轮询结果缓存：pollers 写入，视图读取；切换视图时立即刷新。 */
import type {
  AlertsData, CertsData, DevicesData, FrpData, GrmData, HealthzData,
  NetworkData, OverviewData, ServicesData, UpdatesData,
} from "./types";

export interface SparkCache {
  cpu: number[] | null;
  mem: number[] | null;
  frp: number[] | null;
  ts: number[] | null;
  lab: number[] | null;
}

export const S: {
  overview: OverviewData | null;
  devices: DevicesData | null;
  frp: FrpData | null;
  certs: CertsData | null;
  probes: NetworkData | null;
  services: ServicesData | null;
  releases: GrmData | null;
  updates: UpdatesData | null;
  alerts: AlertsData | null;
  healthz: HealthzData | null;
  spark: SparkCache;
  cur: string;
  /** 服务更新选中的 Provider Tab（"all" 或具体 provider id） */
  updatesProviderTab: string;
  /** 当前选中的目标节点 ID（null = 缺省本机） */
  nodeId: string | null;
  /** 手动 ping 终端输出（全局，轮询重渲染不丢失） */
  pingLog: string | null;
} = {
  overview: null,
  devices: null,
  frp: null,
  certs: null,
  probes: null,
  services: null,
  releases: null,
  updates: null,
  alerts: null,
  healthz: null,
  spark: { cpu: null, mem: null, frp: null, ts: null, lab: null },
  cur: "overview",
  updatesProviderTab: "all",
  nodeId: null,
  pingLog: null,
};

export function setCur(v: string): void {
  S.cur = v;
}

export function setNode(id: string | null): void {
  S.nodeId = id;
  if (typeof window !== "undefined" && typeof window.dispatchEvent === "function") {
    window.dispatchEvent(new CustomEvent("node-change", { detail: { nodeId: id } }));
  }
}
