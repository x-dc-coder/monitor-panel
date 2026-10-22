/** 视图注册表：加能力 = 在这里注册一个新视图。 */
import { esc, pageHead } from "../ui";
import { renderAlerts } from "./alerts";
import { renderCerts } from "./certs";
import { renderDevices } from "./devices";
import { renderLab } from "./lab";
import { renderLinks } from "./links";
import { renderOverview } from "./overview";
import { renderProbes } from "./probes";
import { renderReleases } from "./releases";
import { renderServices } from "./services";
import { renderSettings } from "./settings";
import { renderTaishan } from "./taishan";
import { renderTunnels } from "./tunnels";

/** 视图 id 字面量联合：漏注册/拼错在编译期报错 */
export type ViewId =
  | "overview" | "devices" | "tunnels" | "links"
  | "services" | "releases" | "probes" | "certs" | "alerts"
  | "lab" | "taishan" | "settings";

export type ViewRenderer = () => string;

export const views: Record<ViewId, ViewRenderer> = {
  overview: renderOverview,
  devices: renderDevices,
  tunnels: renderTunnels,
  links: renderLinks,
  services: renderServices,
  releases: renderReleases,
  probes: renderProbes,
  certs: renderCerts,
  alerts: renderAlerts,
  lab: renderLab,
  taishan: renderTaishan,
  settings: renderSettings,
};

export function isViewId(v: string): v is ViewId {
  return v in views;
}

export function placeholder(name: string): string {
  return pageHead("~/console", "未知视图", "—", "") +
    '<div class="placeholder">▸ ' + esc(name) + ' 未注册 · views["' + esc(name) + '"] = () => "…"</div>';
}
