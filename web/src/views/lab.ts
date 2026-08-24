/** 实验视图（lab）：本机实验监控（lab-monitor 转发器上报，issue #11）。
 *  数据源：/api/overview 的 lab 段（转发器 30s 上报，overview 5s 轮询兜底）。 */
import { S } from "../state";
import type { LabSummary } from "../types";
import { esc, pageHead, st, win } from "../ui";

function fmtDur(startTs?: number): string {
  if (!startTs) return "—";
  const diffMs = Date.now() - startTs;
  const m = Math.floor(diffMs / 60000);
  const s = Math.floor((diffMs % 60000) / 1000);
  if (m >= 60) return Math.floor(m / 60) + "h" + (m % 60) + "m";
  if (m > 0) return m + "m" + s + "s";
  return s + "s";
}

function fmtTs(ts?: number): string {
  if (!ts) return "—";
  const d = new Date(ts);
  const p = (n: number): string => String(n).padStart(2, "0");
  return p(d.getMonth() + 1) + "-" + p(d.getDate()) + " " + p(d.getHours()) + ":" + p(d.getMinutes()) + ":" + p(d.getSeconds());
}

function gauge(label: string, valText: string, pct: number | null, cls = ""): string {
  const bar = pct != null
    ? '<div class="lbar"><div class="lfill' + (cls ? " " + cls : "") + '" style="width:' +
      Math.min(pct, 100) + '%"></div></div>'
    : '<div class="lbar empty"></div>';
  return '<div class="lgauge"><div class="lt">' + esc(label) + "</div>" +
    '<div class="lv">' + valText + "</div>" +
    bar + (pct != null ? '<div class="lsub">' + pct + "%</div>" : "") + "</div>";
}

/** 温度进度颜色分级：<50 绿 / 50-80 黄 / >80 红（与告警 tempWarn 对齐） */
function tempCls(c: number | null | undefined): string {
  if (c == null) return "";
  if (c > 80) return "crit";
  if (c >= 50) return "warn";
  return "";
}

export function renderLab(): string {
  const o = S.overview;
  const lab: LabSummary | null = o && o.lab ? o.lab : null;

  const head = pageHead("~/lab", "实验 · lab-monitor", "dc-desktop · 30s", "lab monitor --node dc-desktop --live");

  if (!lab) {
    return head + win("等待接入", "转发器未上报",
      '<div class="empty">本机转发器（lab-monitor/scripts/forward-monitor.sh start）尚未上报，' +
      "数据将在首轮 30s 后出现</div>");
  }

  const exp = lab.experiment;

  // 顶部状态行
  const statusRow =
    '<div class="lab-status">' +
    (lab.monitor_enabled === 1 ? st("ok", "监控中") : st("warn", "已暂停")) +
    '<span class="muted"> 引擎</span> &nbsp;·&nbsp; ' +
    (exp ? st("ok", "实验运行中") : st("off", "空闲")) +
    '<span class="muted"> 实验</span> &nbsp;·&nbsp; 累计 ' + (lab.experiments_total ?? "—") + " 次 &nbsp;·&nbsp; 告警 " +
    (lab.alerts_crit ? '<b style="color:var(--crit)">' + lab.alerts_crit + " crit</b> " : "") +
    (lab.alerts_warn ? '<span style="color:var(--warn)">' + lab.alerts_warn + " warn</span>" : "0") +
    "</div>";

  // GPU 指标卡（语义区分：显存=占比，GPU=百分比，温度=量程+阈值分级）
  const vram = lab.vram_used_mib, vramTot = lab.vram_total_mib;
  const util = lab.gpu_util_percent, temp = lab.gpu_temp_c;
  const vramPct = vram != null && vramTot ? Math.round((vram / vramTot) * 100) : null;
  const gauges =
    '<div class="lab-gauges">' +
    gauge("显存",
      vram != null ? vram + " <small>/ " + (vramTot ?? "—") + " MiB</small>" : "—",
      vramPct) +
    gauge("GPU 利用率",
      util != null ? util + "%" : "—",
      util != null ? Math.round(util) : null) +
    gauge("温度 °C",
      temp != null ? temp + "°C" : "—",
      temp != null ? Math.round(temp) : null,
      tempCls(temp)) +
    "</div>";

  // 当前实验详情
  const expCard = exp
    ? '<div class="lab-exp"><div class="lab-exp-head"><b>▶ 实验 ' + esc(exp.runId || "") + "</b>" +
      '<span class="badge">' + esc(exp.type || "?") + "</span></div>" +
      '<div class="lab-exp-cmd">' + esc(exp.cmd || "—") + "</div>" +
      '<div class="lab-exp-meta"><span>开始 ' + fmtTs(exp.startTs) + "</span>" +
      "<span>已跑 " + fmtDur(exp.startTs) + "</span></div></div>"
    : '<div class="empty">当前无活跃实验（累计 ' + (lab.experiments_total ?? 0) + " 次已完成）</div>";

  // 最近实验历史（从 ended 无直连数据——此处展示 overview 已透出的摘要）
  const histHint =
    '<div class="muted sm" style="margin-top:10px">实验历史明细与 GPU 趋势见本机 lab-monitor 面板；' +
    "此处为远端汇总（转发器 30s 上报）。</div>";

  return head + statusRow + gauges + win("当前实验", "live", expCard + histHint);
}
