/** 公网探测视图：探测表 + 真实数据驱动的终端输出回放。 */
import { fmtMs } from "../fmt";
import { S } from "../state";
import type { ProbeRow } from "../types";
import { esc, pageHead, st, tb, win } from "../ui";

export function renderProbes(): string {
  const probes = S.probes ? S.probes.probes : [];
  const ok = probes.filter((p) => p.reachable).length;
  return pageHead("~/console/probes", "公网探测", ok + "/" + probes.length + " 可达",
    "monitor probes --json · ICMP/TCP/HTTP/DNS · 60s") +
    win("探测结果 · probes", "每 60s 一轮 · asyncio 并发 + 硬超时",
      tb(["探测", "类型", "目标", "延迟", "状态"],
        probes.map((p) =>
          "<tr><td>" + esc(p.name) + '</td><td class="muted">' + esc((p.kind || "").toUpperCase()) +
          '</td><td class="muted">' + esc(p.target) + (p.port ? ":" + esc(String(p.port)) : "") +
          '</td><td class="num">' + fmtMs(p.latency_ms) + "</td><td>" +
          st(p.reachable ? "ok" : "crit", p.reachable ? "可达" : "不可达") + "</td></tr>").join(""),
        "暂无探测数据 · 等待采集")) +
    '<div class="tout show" id="probe-out"></div>';
}

/** 用最近一轮探测结果渲染终端输出（数据驱动，非假数据） */
export function renderProbeOut(): void {
  const box = document.getElementById("probe-out");
  if (!box || !S.probes) return;
  const probes = S.probes.probes;
  const lines: string[] = [];
  for (const p of probes) {
    lines.push('<div class="ln"><span class="ps">$</span> <span class="out">' +
      esc(cmdFor(p)) + "</span></div>");
    if (p.reachable) {
      lines.push('<div class="ln"><span class="ok2">  ✓ ' + esc(p.name) + " · " + fmtMs(p.latency_ms) +
        (p.loss_percent != null ? " · " + p.loss_percent + "% loss" : "") + "</span></div>");
    } else {
      lines.push('<div class="ln"><span class="err2">  ✕ ' + esc(p.name) + " 不可达" +
        (p.error ? " · " + esc(p.error) : "") + "</span></div>");
    }
  }
  if (!lines.length) {
    lines.push('<div class="ln"><span class="out">等待采集…</span></div>');
  }
  lines.push('<div class="ln"><span class="ps">$</span> <span class="out">monitor probes --follow<span class="caret"></span></span></div>');
  box.innerHTML = lines.join("");
}

function cmdFor(p: ProbeRow): string {
  if (p.kind === "icmp") return "ping -c 4 " + p.target;
  if (p.kind === "tcp") return "nc -zv -w 5 " + p.target + " " + (p.port ?? 443);
  if (p.kind === "dns") return "dig +short " + p.target;
  return (p.kind || "http").toUpperCase() + " " + p.target;
}
