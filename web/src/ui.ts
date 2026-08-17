/** UI 组件：终端窗口 / 状态字符 / 表格 / 页面头（全部返回 HTML 字符串）。 */
import type { StatusLevel } from "./types";

export function esc(s: unknown): string {
  return String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" } as Record<string, string>)[c]);
}

const STATUS_CHARS: Record<StatusLevel, string> = { ok: "■", warn: "▲", crit: "✕", off: "□" };

/** 状态字符：■ 在线 / ▲ 降级 / ✕ 故障 / □ 离线 */
export function st(level: StatusLevel, label: string): string {
  return '<span class="st ' + level + '"><span class="ch">' + STATUS_CHARS[level] + "</span>" + esc(label) + "</span>";
}

/** 终端窗口：红黄绿点标题栏 + 内容 */
export function win(title: string, meta: string, inner: string): string {
  return '<section class="win"><div class="titlebar"><span class="dots"><i></i><i></i><i></i></span>' +
    '<span class="tt">' + esc(title) + '</span><span class="meta">' + esc(meta) + '</span></div>' +
    '<div class="body">' + inner + "</div></section>";
}

/** 页面头：crumb + 标题 + live 状态 + 命令提示 */
export function pageHead(crumb: string, title: string, live: string, hint: string): string {
  return '<h1><span class="cr">' + esc(crumb) + "</span>" + esc(title) +
    ' <span class="live">● ' + esc(live) + "</span></h1>" +
    '<div class="hint"><span class="ps">$</span> ' + esc(hint) + "</div>";
}

/** 表格：headers + 行 HTML；空时显示 emptyMsg */
export function tb(headers: string[], rowsHtml: string, emptyMsg?: string): string {
  const head = "<thead><tr>" + headers.map((h) => "<th>" + esc(h) + "</th>").join("") + "</tr></thead>";
  const body = rowsHtml || '<tr><td colspan="' + headers.length + '" class="empty">' +
    esc(emptyMsg || "暂无数据 · 等待采集…") + "</td></tr>";
  return "<table>" + head + "<tbody>" + body + "</tbody></table>";
}

/** 文本按钮（[ping] [刷新] 等） */
export function tbtn(label: string, extraCls = ""): string {
  return '<span class="tbtn ' + extraCls + '">' + esc(label) + "</span>";
}

/** 状态字符串 → 状态级别（ok/warn/crit 之外的归 off） */
export function statusLevel(s: string): StatusLevel {
  return s === "ok" ? "ok" : s === "warn" ? "warn" : s === "crit" ? "crit" : "off";
}
