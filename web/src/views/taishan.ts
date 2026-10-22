/** 泰山代理出网质量监控（taishan，issue #9 Phase 1 S7）。
 *  数据源：/api/taishan?node=dc-pi（dc-pi 探针节点 POST /api/ingest 上报，30s 轮询）。
 *  趋势图：/api/history（score / latency_base_ms / latency_openai_ms / latency_gemini_ms）。 */
import { chart } from "../ascii";
import { api } from "../api";
import { fmtDate, fmtMs, fmtTs } from "../fmt";
import { S } from "../state";
import { table, type TableDef } from "../table";
import type { HistoryData, TaishanCandidate, TaishanData } from "../types";
import { esc, pageHead, st, win } from "../ui";

const TABLE_ID = "taishan";

function stateBadge(state?: string | null): string {
  if (!state) return st("off", "未知");
  if (state === "healthy") return st("ok", "healthy");
  if (state === "all_failed") return st("crit", "all_failed");
  return st("warn", state);
}

function fmtValMs(v?: number | null): string {
  if (v == null) return "—";
  return fmtMs(v);
}

const candidateDef: TableDef = {
  cols: [
    { key: "name", label: "候选节点", fmt: (r: TaishanCandidate) => "<b>" + esc(r.name) + "</b>" },
    { key: "score", label: "加权 score (ms)", fmt: (r: TaishanCandidate) => '<span class="num">' + fmtValMs(r.score) + "</span>" },
    { key: "base", label: "base (ms)", fmt: (r: TaishanCandidate) => '<span class="num">' + fmtValMs(r.base) + "</span>" },
    { key: "openai", label: "openai (ms)", fmt: (r: TaishanCandidate) => '<span class="num">' + fmtValMs(r.openai) + "</span>" },
    { key: "gemini", label: "gemini (ms)", fmt: (r: TaishanCandidate) => '<span class="num">' + fmtValMs(r.gemini) + "</span>" },
  ],
  emptyMsg: "暂无候选节点数据",
};

export function renderTaishan(): string {
  const d: TaishanData | null = S.taishan;
  const head = pageHead("~/taishan", "TaiShan 出网质量", "dc-pi · 30s", "taishan status --node dc-pi --live");

  if (!d) {
    return head + win("等待接入", "节点未上报",
      '<div class="empty">泰山代理探针（dc-pi）尚未上报数据，数据将在首轮 30s 后出现</div>');
  }

  // 状态行：state 灯、active_node、qualified_count、上报 ts
  const tsText = d.ts ? fmtDate(d.ts) + " " + fmtTs(d.ts) : "—";
  const statusRow =
    '<div class="lab-status">' +
    stateBadge(d.state) +
    '<span class="muted"> 状态</span> &nbsp;·&nbsp; 活跃节点 <b>' +
    esc(d.active_node || "—") +
    '</b> &nbsp;·&nbsp; 合格节点 <b>' +
    (d.qualified_count != null ? String(d.qualified_count) : "—") +
    '</b> &nbsp;·&nbsp; 上报时间 <span class="muted">' +
    tsText +
    "</span>" +
    (d.summary ? ' &nbsp;·&nbsp; <span class="muted">' + esc(d.summary) + "</span>" : "") +
    "</div>";

  // KPI 区：四个文本值，score 标注「加权 ms · 越低越好」
  const cands = d.top_candidates || [];
  const activeCand = cands.find((c) => c.name === d.active_node) || cands[0] || null;

  const scoreVal = activeCand && activeCand.score != null ? fmtValMs(activeCand.score) : "—";
  const baseVal = activeCand && activeCand.base != null ? fmtValMs(activeCand.base) : "—";
  const openaiVal = activeCand && activeCand.openai != null ? fmtValMs(activeCand.openai) : "—";
  const geminiVal = activeCand && activeCand.gemini != null ? fmtValMs(activeCand.gemini) : "—";

  const kpis =
    '<div class="lab-gauges">' +
    '<div class="lgauge"><div class="lt">SCORE</div>' +
    '<div class="lv">' + scoreVal + "</div>" +
    '<div class="lsub">加权 ms · 越低越好</div></div>' +
    '<div class="lgauge"><div class="lt">BASE</div>' +
    '<div class="lv">' + baseVal + "</div>" +
    '<div class="lsub">基础测速延迟</div></div>' +
    '<div class="lgauge"><div class="lt">OPENAI</div>' +
    '<div class="lv">' + openaiVal + "</div>" +
    '<div class="lsub">OpenAI 接口延迟</div></div>' +
    '<div class="lgauge"><div class="lt">GEMINI</div>' +
    '<div class="lv">' + geminiVal + "</div>" +
    '<div class="lsub">Gemini 接口延迟</div></div>' +
    "</div>";

  // 趋势图区块（1h / 24h / 7d）
  const trend =
    '<div class="lab-trend">' +
    '<div class="lab-trend-head">' +
    '<span class="lt-title">三轴趋势 · taishan 通道 (dc-pi)</span>' +
    '<span class="lt-ctl"><select id="taishan-range">' +
    '<option value="1h">1h</option><option value="24h" selected>24h</option><option value="7d">7d</option>' +
    '</select> <button type="button" id="taishan-refresh" class="tbtn">[刷新]</button></span>' +
    "</div>" +
    '<div class="lab-trend-grid">' +
    '<div class="lgauge"><div class="lt">Base 延迟</div><div id="taishan-chart-base"><div class="acchart"><pre class="muted">  loading…</pre></div></div></div>' +
    '<div class="lgauge"><div class="lt">OpenAI 延迟</div><div id="taishan-chart-openai"><div class="acchart"><pre class="muted">  loading…</pre></div></div></div>' +
    '<div class="lgauge"><div class="lt">Gemini 延迟</div><div id="taishan-chart-gemini"><div class="acchart"><pre class="muted">  loading…</pre></div></div></div>' +
    "</div></div>";

  // 候选排行表：按 score 升序（越低越好）
  const sortedCandidates = [...cands].sort((a, b) => {
    const sa = a.score != null ? a.score : Number.POSITIVE_INFINITY;
    const sb = b.score != null ? b.score : Number.POSITIVE_INFINITY;
    return sa - sb;
  });

  const candidatesTable = win("候选节点排行", "按 score 升序（越低越好）", table(candidateDef, sortedCandidates, TABLE_ID));

  return head + statusRow + kpis + trend + candidatesTable;
}

/** 趋势图数据加载（taishan 视图渲染后由 bindTaishan 绑定） */
async function loadTaishanChart(boxId: string, metric: string, range: string): Promise<void> {
  const box = document.getElementById(boxId);
  if (!box) return;
  try {
    const j = await api<HistoryData>(
      "/api/history?metric=" + encodeURIComponent(metric) +
      "&group=taishan&node=dc-pi&range=" + encodeURIComponent(range),
    );
    if (j.points.length === 0) {
      box.innerHTML = '<div class="acchart"><pre class="muted">  no data — 等待采样积累（30s 周期）</pre></div>';
      return;
    }
    box.innerHTML = chart(j.points, { title: metric, fmt: (v) => Math.round(v) + "ms" });
  } catch (e: unknown) {
    const msg = e instanceof Error ? e.message : String(e);
    box.innerHTML = '<div class="acchart"><pre class="muted">  ✕ ' + esc(msg) + "</pre></div>";
  }
}

/** taishan 视图交互绑定（main.ts bindView 调用） */
export function bindTaishan(): void {
  const rng = document.getElementById("taishan-range") as HTMLSelectElement | null;
  const ref = document.getElementById("taishan-refresh") as HTMLButtonElement | null;
  if (!rng || !ref) return;
  const load = (): void => {
    const range = rng.value;
    void loadTaishanChart("taishan-chart-base", "latency_base_ms", range);
    void loadTaishanChart("taishan-chart-openai", "latency_openai_ms", range);
    void loadTaishanChart("taishan-chart-gemini", "latency_gemini_ms", range);
  };
  rng.addEventListener("change", load);
  ref.addEventListener("click", load);
  load();
}
