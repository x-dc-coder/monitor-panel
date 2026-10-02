/**
 * 服务更新卡片与 Provider 注册中心（Phase 5：服务更新面板留口子）
 *
 * 架构规范：
 * 1. 统一 UpdateCardModel 规范：无论来自 grm、Docker 镜像源、自建应用检查、App 发布公告，
 *    均归一化为标准的卡片数据模型。
 * 2. Provider 渲染字典扩展：通过 registerUpdateProvider() 注册未来数据源（如 docker、service_check、announcement）。
 * 3. 默认提供 grm 与 generic 渲染适配，支持自定义 badge、customBody、customFooter。
 */

import { fmtDate } from "../fmt";
import type { GrmRepoRow, UpdateItemRow } from "../types";
import { esc, st } from "../ui";

export interface UpdateCardModel {
  /** 数据源标识（如 "grm", "docker", "service_check" 等） */
  provider: string;
  /** 条目唯一 key（如 "owner/repo" 或镜像名） */
  id: string;
  /** 卡片主标题（如仓库名、服务名） */
  title: string;
  /** 副标题（如 slug、命名空间、镜像路径） */
  subtitle?: string;
  /** 卡片跳转链接（如 GitHub 主页、文档地址、管理后台） */
  url?: string;
  /** 版本 tag 或版本号（如 "v1.2.3"） */
  version?: string | null;
  /** 版本详情链接（如 GitHub release notes） */
  versionUrl?: string | null;
  /** 版本名称/标题（如 "Spring Release 2026.1"） */
  versionTitle?: string | null;
  /** 是否新版本标记（高亮 NEW 徽标） */
  isNew?: boolean;
  /** 是否预发布/预览版（pre 徽标） */
  isPrerelease?: boolean;
  /** 是否置顶（置顶优先排在前面） */
  isPinned?: boolean;
  /** 分类标签（如 "monitoring", "proxy", "core" 等） */
  tags: string[];
  /** 发布时间戳（秒） */
  publishedTs?: number | null;
  /** 更新时间戳（秒） */
  updatedTs?: number | null;
  /** 文本摘要/最近提交/变更说明 */
  summary?: string | null;
  /** 扩展属性 */
  extra?: Record<string, unknown>;
}

export interface UpdateProviderRenderer {
  /** 显示名称（用于 Tab 筛选，如 "GitHub Releases (grm)", "Docker Hub"） */
  name: string;
  /** 简短代码 */
  id: string;
  /** 图标或装饰字符（如 "⬥", "🐳", "⚙"） */
  icon?: string;
  /** 自定义卡片主体渲染（可选，未提供时走通用排版） */
  renderBody?: (card: UpdateCardModel, baseUrl?: string) => string;
  /** 自定义徽标渲染（可选） */
  renderBadges?: (card: UpdateCardModel) => string[];
}

/** Provider 注册字典 */
const providerRegistry: Record<string, UpdateProviderRenderer> = {
  grm: {
    id: "grm",
    name: "GitHub Release",
    icon: "⬥",
    renderBadges: (card) => {
      const b: string[] = [];
      if (card.isPinned) b.push(renderBadge("📌", "置顶仓库", "pinned"));
      if (card.isNew) b.push(renderBadge("NEW", "新发布", "new"));
      if (card.isPrerelease) b.push(renderBadge("pre", "预发布", "pre"));
      for (const t of card.tags) b.push(renderBadge(t));
      return b;
    },
  },
  docker: {
    id: "docker",
    name: "Docker 镜像",
    icon: "🐳",
    renderBadges: (card) => {
      const b: string[] = [renderBadge("DOCKER", "容器镜像", "provider-tag")];
      if (card.isPinned) b.push(renderBadge("📌", "置顶", "pinned"));
      if (card.isNew) b.push(renderBadge("NEW", "新镜像构建", "new"));
      for (const t of card.tags) b.push(renderBadge(t));
      return b;
    },
  },
  service_check: {
    id: "service_check",
    name: "自建服务版本",
    icon: "⚙",
    renderBadges: (card) => {
      const b: string[] = [renderBadge("SVC", "自建服务", "provider-tag")];
      if (card.isPinned) b.push(renderBadge("📌", "置顶", "pinned"));
      if (card.isNew) b.push(renderBadge("NEW", "检测到版本更新", "new"));
      for (const t of card.tags) b.push(renderBadge(t));
      return b;
    },
  },
  announcement: {
    id: "announcement",
    name: "应用公告",
    icon: "📢",
    renderBadges: (card) => {
      const b: string[] = [renderBadge("ANNOUNCE", "服务公告", "provider-tag")];
      if (card.isPinned) b.push(renderBadge("📌", "置顶公告", "pinned"));
      if (card.isNew) b.push(renderBadge("NEW", "新公告", "new"));
      return b;
    },
  },
};

/** 注册未来扩展源 */
export function registerUpdateProvider(renderer: UpdateProviderRenderer): void {
  providerRegistry[renderer.id] = renderer;
}

/** 获取已注册的所有 Provider 列表 */
export function getRegisteredProviders(): UpdateProviderRenderer[] {
  return Object.values(providerRegistry);
}

/** 渲染微型徽标（支持高亮 class） */
export function renderBadge(txt: string, title?: string, variant?: "new" | "pre" | "pinned" | "provider-tag"): string {
  const cls = ["badge"];
  if (variant === "new") cls.push("badge-new");
  if (variant === "pre") cls.push("badge-pre");
  if (variant === "pinned") cls.push("badge-pinned");
  if (variant === "provider-tag") cls.push("badge-provider");
  return '<span class="' + cls.join(" ") + '"' + (title ? ' title="' + esc(title) + '"' : "") +
    ">" + esc(txt) + "</span>";
}

/**
 * 将 GrmRepoRow 转换为统一 UpdateCardModel
 */
export function grmRepoToCard(r: GrmRepoRow): UpdateCardModel {
  const rel = r.latest;
  let summaryText = r.readme_summary || null;
  if (!rel && !summaryText && (r.recent_commits as { message?: string }[] | null)?.length) {
    summaryText = (r.recent_commits as { message?: string }[])[0].message || null;
  }

  return {
    provider: "grm",
    id: r.repo_id,
    title: r.display_name || r.owner_slug || r.repo_id,
    subtitle: r.display_name && r.owner_slug ? r.owner_slug : undefined,
    url: r.url || undefined,
    version: rel ? rel.tag : null,
    versionUrl: rel?.url || null,
    versionTitle: rel?.name || null,
    isNew: Boolean(r.is_new),
    isPrerelease: Boolean(rel?.prerelease),
    isPinned: Boolean(r.is_pinned),
    tags: Array.isArray(r.tags) ? r.tags : [],
    publishedTs: rel?.published_ts ?? null,
    updatedTs: r.updated_ts ?? null,
    summary: summaryText,
  };
}

/**
 * 将通用 UpdateItemRow 转换为 UpdateCardModel
 */
export function updateItemToCard(item: UpdateItemRow): UpdateCardModel {
  return {
    provider: item.provider || "generic",
    id: item.item_key,
    title: item.title || item.item_key,
    subtitle: item.item_key !== item.title ? item.item_key : undefined,
    url: item.url || undefined,
    version: item.version,
    versionUrl: item.version_url,
    isNew: Boolean(item.is_new),
    isPrerelease: Boolean(item.prerelease),
    isPinned: Boolean(item.is_pinned),
    tags: Array.isArray(item.tags) ? item.tags : [],
    publishedTs: item.published_ts,
    updatedTs: item.updated_ts,
    summary: item.summary,
  };
}

/**
 * 渲染单个统一 UpdateCard
 */
export function renderUpdateCard(card: UpdateCardModel, baseUrl?: string): string {
  const renderer = providerRegistry[card.provider];
  const badges: string[] = renderer?.renderBadges
    ? renderer.renderBadges(card)
    : [
        ...(card.isPinned ? [renderBadge("📌", "置顶", "pinned")] : []),
        ...(card.isNew ? [renderBadge("NEW", "新发布", "new")] : []),
        ...(card.isPrerelease ? [renderBadge("pre", "预发布", "pre")] : []),
        ...card.tags.map((t) => renderBadge(t)),
      ];

  const badgesHtml = badges.length ? '<div class="rm-tags">' + badges.join("") + "</div>" : "";

  let bodyHtml = "";
  if (renderer?.renderBody) {
    bodyHtml = renderer.renderBody(card, baseUrl);
  } else if (card.version) {
    const vLink = card.versionUrl
      ? '<a class="lnk rm-version" href="' + esc(card.versionUrl) + '" target="_blank" rel="noopener">' +
        esc(card.version) + "</a>"
      : '<span class="rm-version">' + esc(card.version) + "</span>";
    bodyHtml =
      '<div class="rm-rel">' + vLink +
      (card.versionTitle ? '<span class="muted sm">' + esc(card.versionTitle) + "</span>" : "") +
      "</div>" +
      '<div class="muted sm">发布 ' + fmtDate(card.publishedTs) + " · " +
      (card.publishedTs ? esc(new Date(card.publishedTs * 1000).getFullYear() + "") : "—") +
      "</div>";
  } else {
    // 无版本/降级显示
    const summary = card.summary
      ? '<div class="muted sm rm-summary">' + esc(card.summary.slice(0, 120)) + "</div>"
      : '<div class="muted sm">' + st("warn", "无 release") +
        " · README 摘要/最近提交待扩展源就绪后展示</div>";
    bodyHtml = summary;
  }

  const subHtml = card.subtitle ? '<div class="muted sm">' + esc(card.subtitle) + "</div>" : "";
  const targetLink = card.url || baseUrl || "#";
  const headHtml =
    '<div class="rm-head"><div class="rm-title-row">' +
    (renderer?.icon ? '<span class="rm-prov-icon">' + renderer.icon + "</span> " : "") +
    '<a class="lnk" href="' + esc(targetLink) + '" target="_blank" rel="noopener">' +
    esc(card.title) + "</a>" +
    "</div>" + subHtml + "</div>";

  const cardClasses = ["win", "rm-card"];
  if (card.isPinned) cardClasses.push("rm-card-pinned");
  if (card.isNew) cardClasses.push("rm-card-new");

  return '<div class="' + cardClasses.join(" ") + '">' + headHtml + badgesHtml + bodyHtml +
    '<div class="muted sm rm-foot">更新 ' + fmtDate(card.updatedTs) + "</div></div>";
}
