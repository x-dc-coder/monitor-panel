/** 服务台账视图：端口约定驱动的自动发现 + 探活状态（issue #2 方案 C）。 */
import { fmtMs } from "../fmt";
import { S } from "../state";
import type { ServiceRow } from "../types";
import { esc, pageHead, st, tb, win } from "../ui";

/** 链接可达性分级：公网域名可点；内网/回环只展示标记（浏览器无法直达）。 */
function isPrivateHost(host: string): boolean {
  return host === "localhost" ||
    /^127\./.test(host) || /^10\./.test(host) || /^192\.168\./.test(host) ||
    /^172\.(1[6-9]|2\d|3[01])\./.test(host) || /^100\.64\./.test(host) ||
    /^fd/.test(host) || /^fe80/.test(host);
}

function linkCell(r: ServiceRow): string {
  const ipPort = esc(r.tailscale_ip + ":" + r.port);
  if (r.status !== "open") return ipPort;
  // 1) 显式公网直达地址（配置 MONITOR_SERVICES_PUBLIC_URLS）→ 可点
  if (r.public_url) {
    return '<a class="lnk" href="' + esc(r.public_url) + '" target="_blank" rel="noopener">' +
      esc(r.public_url.replace(/^https?:\/\//, "")) + "</a>";
  }
  // 2) 探测 url 存在且 Host 是公网域名 → 可点（探测即公网可达）
  if (r.url) {
    const u = new URL(r.url);
    if (!isPrivateHost(u.hostname)) {
      return '<a class="lnk" href="' + esc(r.url) + '" target="_blank" rel="noopener">' +
        esc(r.url.replace(/^https?:\/\//, "")) + "</a>";
    }
    // 3) 回环/内网地址：不可直达，展示标记
    const tag = /^127\.|^localhost/.test(u.hostname) ? "本机回环" :
      (/^100\.64\./.test(u.hostname) ? "Tailscale 内网" : "内网");
    return ipPort + ' <span class="badge" title="' + esc(r.url) +
      '">' + tag + "</span>";
  }
  return ipPort;
}

export function renderServices(): string {
  const d = S.services;
  const svc = d ? d.services : [];
  const range = d ? d.range : [10000, 19999];
  const sum = d ? d.summary : { total: 0, open: 0, closed: 0, manual: 0 };
  const head = "服务台账 · 约定区 " + range[0] + "–" + range[1] +
    " · 在线 " + sum.open + "/" + sum.total + " · manual " + sum.manual;
  return pageHead("~/console/services", "服务台账", "端口约定驱动自动发现 · 60s",
    "docs/05 端口约定：自定义服务统一绑定 " + range[0] + "–" + range[1] + " · 部署即登记") +
    win("已发现服务", head,
      tb(["设备", "端口", "名称", "协议", "状态", "延迟", "直达"],        svc.map((r) => {
          const statusTxt = r.status === "open" ? "在线" :
            r.status === "closed" ? "离线" : "未知";
          return "<tr><td>" + esc(r.device_name || r.device_id) +
            '</td><td class="num">' + r.port +
            "</td><td>" + esc(r.name || "—") +
            (r.name && r.auto_name && r.name !== r.auto_name
              ? '<div class="muted sm">' + esc(r.auto_name) + "</div>" : "") +
            "</td><td>" + esc(r.protocol || "tcp") +
            (r.http_status ? " " + r.http_status : "") +
            "</td><td>" + st(r.status === "open" ? "ok" : r.status === "closed" ? "crit" : "off",
              statusTxt) +
            '</td><td class="num">' + (r.status === "open" ? fmtMs(r.latency_ms) : "—") +
            "</td><td>" + linkCell(r) + "</td></tr>";
        }).join(""), "暂无服务 · 等待采集（首轮扫描后出现）")) +
    '<div class="legend">■ 自动发现约定区端口 · 连续 3 轮未开放置灰 · 公网直达可点，内网/回环仅标记</div>';
}
