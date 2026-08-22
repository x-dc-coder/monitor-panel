/** Release 监控视图（issue #4）：只读聚合 github-release-monitor（releases.monitor.dc-sy.cn）。
 *  仓库增删/通知渠道等配置在 grm 侧完成，本面板仅聚合展示，无任何配置入口。
 *  无 release 仓库降级展示 README 摘要/最近提交（grm fork 扩展字段就绪前显示占位）。 */
import { fmtDate } from "../fmt";
import { S } from "../state";
import type { GrmRepoRow } from "../types";
import { esc, pageHead, st, win } from "../ui";

function badge(txt: string, title?: string): string {
  return '<span class="badge"' + (title ? ' title="' + esc(title) + '"' : "") +
    ">" + esc(txt) + "</span>";
}

function repoCard(r: GrmRepoRow, baseUrl: string): string {
  const title = r.display_name || r.owner_slug;
  const sub = r.display_name ? '<div class="muted sm">' + esc(r.owner_slug) + "</div>" : "";
  const marks: string[] = [];
  if (r.is_new) marks.push(badge("NEW", "grm 标记：新发布"));
  if (r.latest && r.latest.prerelease) marks.push(badge("pre", "预发布通道"));
  if (r.is_pinned) marks.push(badge("📌", "grm 侧置顶"));
  for (const t of r.tags) marks.push(badge(t));
  const marksHtml = marks.length
    ? '<div class="rm-tags">' + marks.join("") + "</div>" : "";

  let body: string;
  if (r.latest) {
    const rel = r.latest;
    const relLink = rel.url
      ? '<a class="lnk" href="' + esc(rel.url) + '" target="_blank" rel="noopener">' +
        esc(rel.tag) + "</a>"
      : esc(rel.tag);
    body =
      '<div class="rm-rel">' + relLink +
      (rel.name ? '<span class="muted sm">' + esc(rel.name) + "</span>" : "") +
      "</div>" +
      '<div class="muted sm">发布 ' + fmtDate(rel.published_ts) + " · " +
      (rel.published_ts ? esc(new Date(rel.published_ts * 1000).getFullYear() + "") : "—") +
      "</div>";
  } else {
    // 无 release 降级：fork 扩展字段（readmeSummary/recentCommits）就绪前显示占位
    const summary = r.readme_summary
      ? '<div class="muted sm">' + esc(r.readme_summary.slice(0, 120)) + "</div>"
      : '<div class="muted sm">' + st("warn", "无 release") +
        " · README 摘要/最近提交待 grm fork 扩展（A/B/C 系列）就绪后自动展示</div>";
    const commits = (r.recent_commits as { message?: string }[] | null)?.length
      ? '<div class="muted sm">' +
        esc((r.recent_commits as { message?: string }[])[0].message || "") + "</div>"
      : "";
    body = summary + commits;
  }
  // 卡片头部链接跳 grm 面板（仓库详情入口在 grm 侧）；repo url 为 GitHub 主页
  const head =
    '<div class="rm-head"><a class="lnk" href="' + esc(r.url || baseUrl) +
    '" target="_blank" rel="noopener">' + esc(title) + "</a>" + sub + "</div>";
  return '<div class="win rm-card">' + head + marksHtml + body +
    '<div class="muted sm">更新 ' + fmtDate(r.updated_ts) + "</div></div>";
}

export function renderReleases(): string {
  const d = S.releases;
  if (!d) {
    return pageHead("~/console/releases", "Release 监控", "聚合 grm · 5min", "") +
      win("等待数据", "首次采集 ~5s", '<div class="empty">正在连接后端 API…</div>');
  }
  const sum = d.summary;
  const base = d.base_url.replace(/^https?:\/\//, "");
  const head = "仓库 " + sum.total + " · 有 release " + sum.with_release +
    (sum.new_release ? " · 新发布 " + sum.new_release : "");
  const hint = "数据源 github-release-monitor（同机只读 repositories.json）· 5min 采集 · " +
    '配置入口 <a class="lnk" href="' + esc(d.base_url) + '" target="_blank" rel="noopener">' +
    esc(base) + "</a>";

  if (sum.degraded) {
    return pageHead("~/console/releases", "Release 监控", "降级", hint) +
      win("Release 监控", "grm 数据源不可读", '<div class="empty">Release 监控暂不可用' +
        (sum.error ? '<div class="muted sm">' + esc(sum.error) + "</div>" : "") +
        "</div>") +
      '<div class="legend">▲ 仅本区块降级，其余监控不受影响；恢复后下轮采集自动回归</div>';
  }
  if (!d.repos.length) {
    return pageHead("~/console/releases", "Release 监控", head, hint) +
      win("Release 监控", head, '<div class="empty">暂无监控仓库 · 在 grm 侧添加后自动出现</div>');
  }
  return pageHead("~/console/releases", "Release 监控", head, hint) +
    '<div class="grid3 rm-grid">' + d.repos.map((r) => repoCard(r, d.base_url)).join("") +
    "</div>" +
    '<div class="legend">■ 只读聚合 grm（releases.monitor.dc-sy.cn）· NEW=新发布 · pre=预发布 · ' +
    "无 release 仓库显示降级摘要 · 点击仓库名直达</div>";
}
