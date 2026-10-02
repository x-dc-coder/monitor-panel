/** 证书视图：到期清单 + ASCII 进度条 + SAN 标签展示 + 颁发者与证书链状态。 */
import { hbar } from "../ascii";
import { S } from "../state";
import type { CertSummaryItem } from "../types";
import { esc, pageHead, st, statusLevel, tb, win } from "../ui";

/**
 * 提取简化颁发者组织/机构名
 * 如 "CN=R10,O=Let's Encrypt,C=US" -> "Let's Encrypt (R10)"
 */
function fmtIssuer(issuer?: string | null): string {
  if (!issuer) return "—";
  const oMatch = issuer.match(/O=([^,]+)/);
  const cnMatch = issuer.match(/CN=([^,]+)/);
  if (oMatch && cnMatch) return esc(oMatch[1]) + ' <span class="muted sm">(' + esc(cnMatch[1]) + ")</span>";
  if (oMatch) return esc(oMatch[1]);
  if (cnMatch) return esc(cnMatch[1]);
  return esc(issuer.slice(0, 30));
}

/**
 * 渲染 SAN 列表（折叠/标签化展示）
 */
function renderSans(c: CertSummaryItem): string {
  const sans = c.sans && c.sans.length ? c.sans : [c.domain];
  // 如果只有一个域名且就是主域名，直接展示
  if (sans.length === 1 && sans[0] === c.domain) {
    return '<span class="muted sm">' + esc(c.domain) + "</span>";
  }
  // 多个 SAN，用标签展示
  const tags = sans.map((s) => '<span class="badge cert-san-tag">' + esc(s) + "</span>");
  return '<div class="cert-san-list">' + tags.join(" ") + "</div>";
}

/**
 * 渲染证书链状态
 */
function renderChain(c: CertSummaryItem): string {
  if (c.chain_verified === true) {
    return '<span class="ok2" title="证书链完整校验通过">完整 ✓</span>';
  }
  if (c.chain_verified === false) {
    return '<span class="err2" title="证书链校验失败或自签">异常 ✕</span>';
  }
  return '<span class="muted" title="未校验/本地证书">未知</span>';
}

/**
 * 计算进度条颜色样式：
 * - <0 天：已过期深红警告
 * - <7 天：crit 亮红
 * - <30 天：warn 琥珀黄
 * - >=30 天：正常磷光青
 */
function getProgressClass(daysLeft: number): string {
  if (daysLeft <= 0) return "red dark-crit";
  if (daysLeft < 7) return "red";
  if (daysLeft < 30) return "amber";
  return "";
}

export function renderCerts(): string {
  const certs = S.certs ? S.certs.certs : [];
  const warn = certs.filter((c) => c.status === "warn").length;
  const crit = certs.filter((c) => c.status === "crit").length;
  const expired = certs.filter((c) => c.days_left <= 0).length;

  const summaryText =
    warn + " warn · " + crit + " crit" +
    (expired > 0 ? ' · <span style="color:var(--crit);font-weight:700">' + expired + " 已过期</span>" : "");

  return pageHead("~/console/certs", "证书", summaryText,
    "TLS 443 SNI 主动探测 + openssl 读取 · 1h · 90 天签发周期") +
    win("证书清单 · SSL / TLS", "SAN 备用域名 · 颁发者 · 链校验 · 剩余天数",
      tb(["DOMAIN / SAN", "颁发者", "到期时间", "剩余天数", "进度", "链状态", "状态"],
        certs.map((c) => {
          const filled = Math.max(0, Math.min(20, Math.round((c.days_left / 90) * 20)));
          const barCls = getProgressClass(c.days_left);
          const daysText = c.days_left <= 0
            ? '<b style="color:var(--crit)">已过期 ' + Math.abs(Math.round(c.days_left)) + " 天</b>"
            : Math.round(c.days_left) + " 天";

          const domainHtml =
            '<div class="cert-domain-cell"><b>' + esc(c.domain) + "</b>" +
            renderSans(c) + "</div>";

          return "<tr>" +
            "<td>" + domainHtml + "</td>" +
            "<td>" + fmtIssuer(c.issuer) + "</td>" +
            '<td class="muted num">' + esc(c.not_after || "—") + "</td>" +
            '<td class="num">' + daysText + "</td>" +
            "<td>" + hbar(filled, 20, barCls) + "</td>" +
            "<td>" + renderChain(c) + "</td>" +
            "<td>" + st(statusLevel(c.status), c.status) + "</td>" +
            "</tr>";
        }).join(""), "暂无证书数据 · 等待采集")) +
    '<div class="legend">▓ 已消耗 · ░ 剩余 · 20 格 = 90 天 · &lt;30 天 warn(黄) · &lt;7 天 crit(红) · &le;0 天 已过期(深红) · 域名含 SAN 备用列表</div>';
}
