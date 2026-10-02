/**
 * 服务更新视图（Phase 5：服务更新面板留口子）
 *
 * 聚合 github-release-monitor (grm) 与未来多源服务更新 (Docker / 自建应用 / 公告等)。
 * - 支持 Provider Tab 筛选与分类
 * - 支持置顶卡片排版（isPinned 置顶优先）
 * - 支持 NEW 徽标高亮
 * - 面板只读聚合，配置入口在各源管理端完成
 */

import { S } from "../state";
import {
  getRegisteredProviders,
  grmRepoToCard,
  renderUpdateCard,
  updateItemToCard,
  type UpdateCardModel,
} from "../updates/cards";
import { esc, pageHead, win } from "../ui";

export function renderReleases(): string {
  const grmData = S.releases;
  const updatesData = S.updates;

  // 聚合多源模型：优先使用 updatesData.items，如果尚未拉取到，则回退使用 grmData.repos
  const cardList: UpdateCardModel[] = [];
  const registeredProviders = getRegisteredProviders();

  if (updatesData && updatesData.items && updatesData.items.length > 0) {
    for (const item of updatesData.items) {
      cardList.push(updateItemToCard(item));
    }
  } else if (grmData && grmData.repos) {
    for (const repo of grmData.repos) {
      cardList.push(grmRepoToCard(repo));
    }
  }

  const currentTab = S.updatesProviderTab || "all";
  const baseUrl = grmData ? grmData.base_url : "https://releases.monitor.dc-sy.cn";
  const baseHost = baseUrl.replace(/^https?:\/\//, "");

  // 如果两者都没就绪
  if (!grmData && !updatesData) {
    return pageHead("~/console/releases", "服务更新", "聚合 Releases · 5min", "") +
      win("等待数据", "首次采集 ~5s", '<div class="empty">正在连接更新聚合服务…</div>');
  }

  // 降级判断：grm 降级或者 updates 降级
  const isDegraded = Boolean(grmData?.summary.degraded || updatesData?.summary.degraded);
  const errorMsg = grmData?.summary.error || updatesData?.summary.error;

  const hint = "数据源多源更新（支持 grm / Docker / 自建服务检查）· 5min 采集 · " +
    '配置入口 <a class="lnk" href="' + esc(baseUrl) + '" target="_blank" rel="noopener">' +
    esc(baseHost) + "</a>";

  if (isDegraded && cardList.length === 0) {
    return pageHead("~/console/releases", "服务更新", "降级", hint) +
      win("服务更新", "数据源不可读", '<div class="empty">服务更新暂不可用' +
        (errorMsg ? '<div class="muted sm">' + esc(errorMsg) + "</div>" : "") +
        "</div>") +
      '<div class="legend">▲ 仅本区块降级，其余监控不受影响；恢复后下轮采集自动回归</div>';
  }

  // 统计指标
  const totalCards = cardList.length;
  const newCount = cardList.filter((c) => c.isNew).length;
  const pinnedCount = cardList.filter((c) => c.isPinned).length;
  const withVersionCount = cardList.filter((c) => c.version).length;

  const head = "条目 " + totalCards +
    " · 有版本 " + withVersionCount +
    (newCount ? " · 新发布 " + newCount : "") +
    (pinnedCount ? " · 置顶 " + pinnedCount : "");

  // Provider Tab 筛选条
  // 获取当前实际存在的 providers（从 cardList 中提取，并与注册字典比对）
  const activeProviders = Array.from(new Set(cardList.map((c) => c.provider)));
  const tabProviders = registeredProviders.filter(
    (p) => activeProviders.includes(p.id) || p.id === "grm"
  );

  const tabsHtml =
    '<div class="rm-tabs">' +
    '<button type="button" class="tbtn rm-tab-btn' + (currentTab === "all" ? " on" : "") + '" data-provider="all">' +
    "全部 (" + totalCards + ")</button>" +
    tabProviders.map((p) => {
      const cnt = cardList.filter((c) => c.provider === p.id).length;
      const on = currentTab === p.id ? " on" : "";
      return '<button type="button" class="tbtn rm-tab-btn' + on + '" data-provider="' + esc(p.id) + '">' +
        (p.icon ? esc(p.icon) + " " : "") + esc(p.name) + " (" + cnt + ")</button>";
    }).join("") +
    "</div>";

  // 按 Tab 筛选卡片
  let filteredCards = cardList;
  if (currentTab !== "all") {
    filteredCards = filteredCards.filter((c) => c.provider === currentTab);
  }

  // 排序规则：置顶卡片优先（isPinned=true 排前），其次按 isNew 排前，再按 publishedTs 倒序
  filteredCards.sort((a, b) => {
    if ((b.isPinned ? 1 : 0) !== (a.isPinned ? 1 : 0)) {
      return (b.isPinned ? 1 : 0) - (a.isPinned ? 1 : 0);
    }
    if ((b.isNew ? 1 : 0) !== (a.isNew ? 1 : 0)) {
      return (b.isNew ? 1 : 0) - (a.isNew ? 1 : 0);
    }
    return (b.publishedTs || 0) - (a.publishedTs || 0);
  });

  const cardsContent = filteredCards.length
    ? '<div class="grid3 rm-grid">' +
      filteredCards.map((c) => renderUpdateCard(c, baseUrl)).join("") +
      "</div>"
    : '<div class="empty">暂无该分类的更新条目</div>';

  return pageHead("~/console/releases", "服务更新", head, hint) +
    tabsHtml +
    cardsContent +
    '<div class="legend">■ 多源聚合（GitHub Release / Docker 镜像 / 自建服务检测）· NEW=新版本高亮 · 📌=置顶 · 点击项目直达</div>';
}

/** 绑定 Tab 切换事件 */
export function bindReleases(): void {
  document.querySelectorAll<HTMLButtonElement>(".rm-tab-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const p = btn.dataset.provider || "all";
      S.updatesProviderTab = p;
      const ev = new CustomEvent("table-change", { detail: { view: "releases" } });
      document.dispatchEvent(ev);
    });
  });
}
