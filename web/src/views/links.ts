/** 链路质量视图：8899 agent 表格 + 带宽 ASCII 条 + 延迟字符画趋势。 */
import { api } from "../api";
import { chart } from "../ascii";
import { fmtMs, fmtPct } from "../fmt";
import { S } from "../state";
import type { HistoryData } from "../types";
import { esc, pageHead, st, tb, win } from "../ui";

export function renderLinks(): string {
  const frp = S.frp;
  const link = frp ? frp.link : [];
  const opts = link.map((l) => '<option value="' + esc(l.device_id) + '">' +
    esc(l.device_id + (l.hostname ? " · " + l.hostname : "")) + "</option>").join("");
  return pageHead("~/console/links", "frp 链路质量", "8899 agent 已接入",
    "curl 127.0.0.1:8899/api/status -H 'Authorization: Bearer ***' · 10s") +
    win("链路质量 · 8899 agent", "latency / packet_loss / frpc·frps / 带宽利用率",
      tb(["设备", "tailscale 对照", "延迟", "丢包", "frpc", "frps", "上行利用率", "下行利用率"],
        link.map((l) => {
          const up = l.bandwidth_up_kbps || 0;
          const dn = l.bandwidth_down_kbps || 0;
          const upF = Math.min(10, Math.round((up / 2048) * 10));
          const dnF = Math.min(10, Math.round((dn / 2048) * 10));
          const barCls = (v: number) => (v / 2048 > 0.75 ? "red" : "");
          const bar = (filled: number, cls: string) => {
            let s = '<span class="abar ' + cls + '">';
            for (let i = 0; i < 10; i++) s += i < filled ? "<i>█</i>" : '<i class="off">░</i>';
            return s + "</span>";
          };
          return "<tr><td>" + esc(l.device_id) + '</td><td class="muted">' + esc(l.hostname || "—") +
            '</td><td class="num">' + fmtMs(l.latency_ms) + '</td><td class="num">' +
            fmtPct(l.packet_loss_percent) + "</td><td>" +
            st(l.frpc_alive ? "ok" : "crit", l.frpc_alive ? "在线" : "DOWN") + "</td><td>" +
            st(l.frps_alive ? "ok" : "crit", l.frps_alive ? "在线" : "DOWN") + "</td>" +
            '<td class="num">' + bar(upF, barCls(up)) + " " +
            (l.bandwidth_up_kbps != null ? l.bandwidth_up_kbps + "k" : "—") + '</td><td class="num">' +
            bar(dnF, barCls(dn)) + " " +
            (l.bandwidth_down_kbps != null ? l.bandwidth_down_kbps + "k" : "—") + "</td></tr>";
        }).join(""), "暂无 8899 agent 数据 · 未配置 token 或 agent 离线")) +
    '<div class="legend">■ 10 格 = 2048 kb/s 上限 · 红条 = 使用率 &gt; 75% · frpc/frps 来自 8899 状态机</div>' +
    win("延迟趋势 · ASCII 字符画", "history 降采样 · 选择设备与范围",
      '<div class="subbar" style="padding:8px 14px;margin:0">' +
      '<select class="tbtn" id="link-dev">' + opts + "</select>" +
      '<select class="tbtn" id="link-range">' +
      '<option value="1h">1h</option><option value="24h" selected>24h</option>' +
      '<option value="7d">7d</option><option value="30d">30d</option></select>' +
      '<span class="tbtn" id="link-refresh">[刷新]</span></div>' +
      '<div id="link-chart"><div class="acchart"><pre class="muted">  ▸ 选择设备后渲染趋势</pre></div></div>');
}

/** 绑定趋势控件（视图渲染后调用） */
export function bindLinks(): void {
  const devSel = document.getElementById("link-dev") as HTMLSelectElement | null;
  const rngSel = document.getElementById("link-range") as HTMLSelectElement | null;
  const box = document.getElementById("link-chart");
  if (!devSel || !rngSel || !box) return;

  const load = async (): Promise<void> => {
    if (!devSel.value) return;
    box.innerHTML = '<div class="acchart"><pre class="muted">  loading…</pre></div>';
    try {
      const j = await api<HistoryData>("/api/history?metric=" + encodeURIComponent("latency_ms." + devSel.value) +
        "&group=frp_link&range=" + rngSel.value);
      box.innerHTML = chart(j.points, { title: "latency_ms." + devSel.value, fmt: (v) => v + "ms" });
    } catch (e) {
      box.innerHTML = '<div class="acchart"><pre class="muted">  ✕ ' + esc((e as Error).message) + "</pre></div>";
    }
  };
  devSel.addEventListener("change", () => void load());
  rngSel.addEventListener("change", () => void load());
  const ref = document.getElementById("link-refresh");
  if (ref) ref.addEventListener("click", () => void load());
  void load();
}
