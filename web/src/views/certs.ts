/** 证书视图：到期清单 + ASCII 进度条（20 格 = 90 天）。 */
import { hbar } from "../ascii";
import { S } from "../state";
import { esc, pageHead, st, tb, win } from "../ui";

function statusLevel(s: string): "ok" | "warn" | "crit" | "off" {
  return s === "ok" ? "ok" : s === "warn" ? "warn" : s === "crit" ? "crit" : "off";
}

export function renderCerts(): string {
  const certs = S.certs ? S.certs.certs : [];
  const warn = certs.filter((c) => c.status === "warn").length;
  const crit = certs.filter((c) => c.status === "crit").length;
  return pageHead("~/console/certs", "证书", warn + " warn · " + crit + " crit",
    "openssl x509 -in /etc/letsencrypt/live/*/cert.pem -noout -enddate · 1h") +
    win("证书清单 · letsencrypt", "90 天签发周期 · 进度条 = 剩余天数",
      tb(["DOMAIN", "到期", "剩余", "进度", "状态"],
        certs.map((c) => {
          const filled = Math.max(0, Math.min(20, Math.round((c.days_left / 90) * 20)));
          const cls = c.status === "crit" ? "red" : c.status === "warn" ? "amber" : "";
          return "<tr><td>" + esc(c.domain) + '</td><td class="muted">' + esc(c.not_after || "—") +
            '</td><td class="num">' + Math.round(c.days_left) + ' 天</td><td>' +
            hbar(filled, 20, cls) + "</td><td>" + st(statusLevel(c.status), c.status) + "</td></tr>";
        }).join(""), "暂无证书数据 · 等待采集")) +
    '<div class="legend">▓ 已消耗 · ░ 剩余 · 20 格 = 90 天 · &lt;30 天 warn / &lt;7 天 crit</div>';
}
