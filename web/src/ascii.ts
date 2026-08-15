/**
 * ASCII 图形渲染层（A1 终端 DNA 的核心）——纯函数、无 DOM 依赖、可单测。
 * sparkline: 8 级块字符单行趋势 ▁▂▃▄▅▆▇█
 * hbar:      水平条（█ 实 / ░ 空）
 * chart:     多行字符画趋势图（Y 轴 3 档刻度 + 时间轴）
 */
import type { HistoryPoint } from "./types";

const BLOCKS = ["▁", "▂", "▃", "▄", "▅", "▆", "▇", "█"] as const;

/** 单行 8 级 sparkline。cls: "" | "amber" | "violet" | "dim" */
export function sparkline(vals: number[] | null | undefined, cls = ""): string {
  if (!vals || vals.length === 0) return '<span class="blocks dim">—</span>';
  const mx = Math.max(...vals);
  const mn = Math.min(...vals);
  const r = mx - mn || 1;
  const body = vals
    .map((v) => {
      const i = Math.min(7, Math.max(0, Math.round(((v - mn) / r) * 7)));
      return "<i>" + BLOCKS[i] + "</i>";
    })
    .join("");
  return '<span class="blocks ' + cls + '">' + body + "</span>";
}

/** ASCII 水平条：filled 格亮（█），其余暗（░）。cls: "" | "amber" | "red" */
export function hbar(filled: number, total: number, cls = ""): string {
  const f = Math.max(0, Math.min(total, Math.round(filled)));
  let s = '<span class="abar ' + cls + '">';
  for (let i = 0; i < total; i++) {
    s += i < f ? "<i>█</i>" : '<i class="off">░</i>';
  }
  return s + "</span>";
}

export interface ChartOptions {
  title?: string;
  /** 数值格式化（如 (v) => v + "ms"） */
  fmt?: (v: number) => string;
}

/**
 * 多行字符画趋势图：默认 64 列 × 10 行（自适应列数）。
 * 列数超出 W 时按步长抽稀（均值降采样），与后端 /api/history 桶语义一致。
 */
export function chart(pts: HistoryPoint[] | null | undefined, opts: ChartOptions = {}): string {
  if (!pts || pts.length === 0) {
    return '<div class="acchart"><pre class="muted">  no data — 等待采样积累</pre></div>';
  }
  const W = Math.min(64, Math.max(24, pts.length));
  const H = 10;
  const values = pts.map((p) => p.value);
  const mx = Math.max(...values);
  const mn = Math.min(...values);
  const span = mx - mn || Math.max(Math.abs(mx) * 0.1, 1);
  const maxV = Math.ceil((mx + span * 0.08) * 100) / 100;
  const minV = Math.floor((mn - span * 0.08) * 100) / 100;

  // 抽稀：步长取整，每步取均值
  const step = Math.max(1, Math.ceil(pts.length / W));
  const cols: { ts: number; v: number }[] = [];
  for (let i = 0; i < pts.length; i += step) {
    const chunk = pts.slice(i, i + step);
    cols.push({
      ts: chunk[0].ts,
      v: chunk.reduce((a, b) => a + b.value, 0) / chunk.length,
    });
  }

  const rows: string[] = [];
  for (let row = 0; row < H; row++) {
    const hi = minV + ((maxV - minV) * (H - row)) / H;
    const lo = minV + ((maxV - minV) * (H - row - 1)) / H;
    let line = "";
    for (const c of cols) {
      const inBand = c.v >= lo && c.v <= hi;
      const onBoundary = Math.abs(c.v - hi) < ((maxV - minV) / H) * 0.35;
      line += inBand ? (onBoundary ? "█" : "▓") : " ";
    }
    rows.push(line);
  }

  const yLbl = (v: number) => (opts.fmt ? opts.fmt(v) : v.toFixed(1)).padStart(9, " ");
  const t0 = fmtShort(cols[0].ts);
  const t1 = fmtShort(cols[cols.length - 1].ts);
  const mid = (yLbl((maxV + minV) / 2) + " ").padEnd(10, " ");
  const pad = "         ";

  let out = '<div class="acchart"><div class="ac-title">' + esc(opts.title || "") +
    " · " + esc(t0) + " → " + esc(t1) + " · 纵向 10 行</div><pre>";
  out += '<span class="ac-axis">' + yLbl(maxV) + ' ┤</span><span class="ac-curve">' + rows[0] + "</span>\n";
  for (let i = 1; i < H - 1; i++) {
    const lbl = i === Math.floor(H / 2) ? mid.slice(0, 9) : " ".repeat(9);
    out += '<span class="ac-axis">' + lbl + ' ┤</span><span class="ac-curve">' + rows[i] + "</span>\n";
  }
  out += '<span class="ac-axis">' + yLbl(minV) + ' ┘</span><span class="ac-curve">' + rows[H - 1] + "</span>\n";
  out += '<span class="ac-axis">' + pad + ' └' + "─".repeat(Math.min(W, cols.length)) + "\n";
  out += pad + "  " + esc(t0) + " ".repeat(Math.max(4, cols.length - 10)) + esc(t1) + "</span></pre></div>";
  return out;
}

function fmtShort(ts: number): string {
  const d = new Date(ts * 1000);
  const p = (n: number) => String(n).padStart(2, "0");
  return p(d.getHours()) + ":" + p(d.getMinutes());
}

function esc(s: string): string {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" } as Record<string, string>)[c]);
}
