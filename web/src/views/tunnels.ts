/** 隧道视图：每条隧道一张端口转发机制图 + 流量表。 */
import { fmtBytes } from "../fmt";
import { S } from "../state";
import { esc, pageHead, st, tb, win } from "../ui";

export function renderTunnels(): string {
  const frp = S.frp;
  const tunnels = frp ? frp.tunnels : [];
  const online = tunnels.filter((t) => t.status === "online").length;
  const head = pageHead("~/console/tunnels", "隧道", online + "/" + tunnels.length + " 在线",
    "frps /api/serverinfo + /api/proxy/tcp · 30s" + (frp && frp.degraded ? " · dashboard 降级（8899 兜底）" : "")) +
    '<div class="subbar"><span class="sep">▸</span> 每条隧道 = 一条公网端口 → 内网地址的转发链路</div>';

  if (!tunnels.length) {
    return head + win("隧道 · tunnels", "frps :7500",
      '<div class="empty">暂无隧道 · dashboard 不可用或未采集</div>');
  }
  return head + tunnels.map((t) =>
    win(t.tunnel_id + " · " + (t.type || "?") + " /" + (t.remote_port ?? "?"),
      "traffic today · 30s" + (t.status === "stale" ? " · ⚠ 数据陈旧（frpc 长期未连，非实时）" : ""),
      '<div class="flow">' +
      '<span class="node">0.0.0.0:<b>' + esc(String(t.remote_port ?? "?")) + '</b><span class="tag">公网</span></span>' +
      '<span class="arr">───▶<span class="lab">' + esc(t.type || "?") + "</span>───▶</span>" +
      '<span class="node"><b>' + esc(t.local_addr || "?") + '</b><span class="tag">内网</span></span>' +
      "</div>" +
      tb(["状态", "连接数", "今日入流量", "今日出流量"],
        "<tr><td>" + st(t.status === "online" ? "ok" : t.status === "stale" ? "off" : "crit",
          t.status === "stale" ? "已断开(陈旧)" : t.status) +
        '</td><td class="num">' + esc(String(t.cur_conns ?? "—")) + '</td><td class="num">' +
        fmtBytes(t.traffic_in) + '</td><td class="num">' + fmtBytes(t.traffic_out) + "</td></tr>")))
    .join("");
}
