/**
 * 通用 DataTable 组件（A1 精工终端 · 2026-08-17）
 *
 * 配置驱动表格：分页 + 列头下拉筛选 + 分组聚合统计。
 * - 分页：每页 10/20/50 可切换，页码导航，筛选后自动回到第 1 页
 * - 筛选：filterable 列生成下拉（值自动去重），多列组合
 * - 聚合：groupBy 维度显示统计条（如 设备：cloud 9 · DC_DC 7），点击分组过滤
 * - 状态保持：模块级持久状态（按视图隔离），轮询重渲染不丢
 *
 * 用法：
 *   const def = { cols, groupBy?: "device", pageSize?: 10 };
 *   html = table(def, rows, "services")     // 渲染
 *   bindTable(def, rows, "services", onDataChange)  // 挂事件
 */
import { esc } from "./ui";

export interface ColDef {
  key: string;
  label: string;
  fmt?: (row: any) => string;   // 单元格格式化（默认 String(row[key])）
  filterable?: boolean;         // 参与列头下拉筛选
  filterMap?: Record<string, string>;  // 筛选下拉值 → 显示名（如 0→"未解决"）
}

export interface TableDef {
  cols: ColDef[];
  groupBy?: string;             // 聚合统计维度（列 key）
  pageSize?: number;            // 默认每页行数（10/20/50）
  emptyMsg?: string;
}

export interface TableState {
  page: number;
  pageSize: number;
  filters: Record<string, string>;  // colKey → 筛选值
  group: string | null;             // 当前分组过滤值
  search: string;
}

/** 视图级持久状态（WeakMap 无泄漏；key 用对象而非字符串避免跨视图串扰） */
const states = new WeakMap<object, TableState>();
const viewKeys = new Map<string, object>();

function stateFor(viewId: string): TableState {
  let key = viewKeys.get(viewId);
  if (!key) {
    key = {};
    viewKeys.set(viewId, key);
  }
  let s = states.get(key);
  if (!s) {
    s = { page: 1, pageSize: 10, filters: {}, group: null, search: "" };
    states.set(key, s);
  }
  return s;
}

const PAGE_SIZES = [10, 20, 50];

/** 应用筛选：多列下拉 + 分组 + 搜索（搜索匹配任意可读列） */
function applyFilter<T>(
  rows: T[],
  def: TableDef,
  s: TableState,
): T[] {
  let out = rows;
  for (const c of def.cols) {
    const fv = s.filters[c.key];
    if (fv && fv !== "__all__") {
      out = out.filter((r) => String((r as any)[c.key]) === fv);
    }
  }
  if (s.group && def.groupBy) {
    out = out.filter((r) => String((r as any)[def.groupBy!]) === s.group);
  }
  if (s.search) {
    const q = s.search.toLowerCase();
    out = out.filter((r) =>
      def.cols.some((c) =>
        String(c.fmt ? c.fmt(r) : (r as any)[c.key] ?? "").toLowerCase().includes(q)));
  }
  return out;
}

/** 列可选值（去重，按出现顺序） */
function uniqueVals<T>(rows: T[], key: string): string[] {
  const seen: string[] = [];
  for (const r of rows) {
    const v = String((r as any)[key] ?? "");
    if (v && !seen.includes(v)) seen.push(v);
  }
  return seen;
}

/** 分组统计：{值: 计数}（按出现顺序） */
function groupCounts<T>(rows: T[], key: string): [string, number][] {
  const m = new Map<string, number>();
  for (const r of rows) {
    const v = String((r as any)[key] ?? "—");
    m.set(v, (m.get(v) || 0) + 1);
  }
  return [...m.entries()];
}

function pageRows<T>(rows: T[], s: TableState): { items: T[]; total: number; pages: number } {
  const total = rows.length;
  const pages = Math.max(1, Math.ceil(total / s.pageSize));
  if (s.page < 1) s.page = 1;
  if (s.page > pages) s.page = pages;
  const start = (s.page - 1) * s.pageSize;
  return { items: rows.slice(start, start + s.pageSize), total, pages };
}

/**
 * 渲染表格 + 筛选条 + 聚合统计 + 分页导航。
 * viewId 用于隔离状态；返回 HTML 字符串。
 */
export function table<T>(def: TableDef, rows: T[], viewId: string): string {
  const s = stateFor(viewId);
  const filtered = applyFilter(rows, def, s);
  const paged = pageRows(filtered, s);
  const total = paged.total, pages = paged.pages;
  const pageRowsOut = paged.items;

  // 筛选条：可筛选列下拉
  let bar = "";
  const filterCols = def.cols.filter((c) => c.filterable);
  if (filterCols.length) {
    bar = '<div class="tbar">';
    for (const c of filterCols) {
      const cur = s.filters[c.key] || "__all__";
      const opts = ['<option value="__all__">' + esc(c.label) + " · 全部</option>"]
        .concat(uniqueVals(rows, c.key).map((v) =>
          '<option value="' + esc(v) + '"' + (cur === v ? " selected" : "") + ">" +
          esc(c.filterMap ? (c.filterMap[v] || v) : v) + "</option>")).join("");
      bar += '<label class="tfilter">' + esc(c.label) + " <select data-fk='" + c.key + "'>" +
        opts + "</select></label>";
    }
    bar += '<span class="tcnt">' + total + " 条</span></div>";
  }

  // 聚合统计条（点击分组过滤）
  let agg = "";
  if (def.groupBy) {
    const gcs = groupCounts(filtered, def.groupBy);
    agg = '<div class="tagg">聚合 <span class="muted">' +
      esc(def.groupBy) + "</span>：" + gcs.map(([v, n]) => {
        const on = s.group === v;
        return '<button class="tag' + (on ? " on" : "") + '" data-g="' + esc(v) + '">' +
          esc(v) + " <b>" + n + "</b></button>";
      }).join(" ") + (s.group ? ' <button class="tag clear" data-g="">✕ 清除</button>' : "") +
      "</div>";
  }

  // 表头 + 行
  const head = "<thead><tr>" + def.cols.map((c) => "<th>" + esc(c.label) + "</th>").join("") +
    "</tr></thead>";
  const body = pageRowsOut.length
    ? pageRowsOut.map((r) => "<tr>" + def.cols.map((c) => {
        const v = c.fmt ? c.fmt(r) : String((r as any)[c.key] ?? "");
        return "<td>" + v + "</td>";
      }).join("") + "</tr>").join("")
    : '<tr><td colspan="' + def.cols.length + '" class="empty">' +
      esc(def.emptyMsg || "无数据 · 试试调整筛选") + "</td></tr>";

  // 分页导航
  let pager = "";
  if (total > 0) {
    pager = '<div class="tpager">' +
      '<button class="pg" data-p="prev"' + (s.page <= 1 ? " disabled" : "") + ">‹ 上一页</button>" +
      '<span class="pg-info">第 <b>' + s.page + "</b> / " + pages + " 页 · " + total + " 条</span>" +
      '<button class="pg" data-p="next"' + (s.page >= pages ? " disabled" : "") + ">下一页 ›</button>" +
      '<label class="pg-size">每页 <select data-ps>' +
      PAGE_SIZES.map((n) => '<option value="' + n + '"' + (n === s.pageSize ? " selected" : "") +
        ">" + n + "</option>").join("") + "</select> 行</label></div>";
  }

  // 最外层 wrap 唯一带 data-tb（事件绑定锚点，内部区域不再重复标记）
  return '<div class="twrap" data-tb="' + viewId + '">' + bar + agg +
    "<table>" + head + "<tbody>" + body + "</tbody></table>" + pager +
    "</div>";
}

/** 清空某视图状态（视图重进时可选调用） */
export function resetTable(viewId: string): void {
  const key = viewKeys.get(viewId);
  if (key) states.delete(key);
}

/**
 * 视图级入口：渲染后由 main.ts 调用，把当前视图的表格定义与数据
 * 注册到通用绑定（事件委托查找当前视图 DOM）。
 * 各视图在自己的 render 函数里调用 table() 渲染，这里负责挂事件。
 */
export function bindTableForView(viewId: string): void {
  const root = document.querySelector(".view");
  if (!root) return;
  // 只绑定最外层 twrap（唯一 data-tb），避免内部区域重复绑定导致事件多次触发
  const wrap = root.querySelector("[data-tb]");
  if (!wrap) return;
  const s = stateFor(viewId);
  const rerender = (): void => {
    const ev = new CustomEvent("table-change", { detail: { view: viewId } });
    document.dispatchEvent(ev);
  };
  wrap.querySelectorAll("select[data-fk]").forEach((sel) => {
    sel.addEventListener("change", () => {
      s.filters[(sel as HTMLSelectElement).dataset.fk!] = (sel as HTMLSelectElement).value;
      s.page = 1;
      rerender();
    });
  });
  wrap.querySelectorAll("select[data-ps]").forEach((sel) => {
    sel.addEventListener("change", () => {
      s.pageSize = Number((sel as HTMLSelectElement).value);
      s.page = 1;
      rerender();
    });
  });
  wrap.querySelectorAll("button[data-p]").forEach((btn) => {
    btn.addEventListener("click", () => {
      if (btn.hasAttribute("disabled")) return;
      s.page += (btn as HTMLElement).dataset.p === "next" ? 1 : -1;
      rerender();
    });
  });
  wrap.querySelectorAll("button[data-g]").forEach((btn) => {
    btn.addEventListener("click", () => {
      s.group = (btn as HTMLElement).dataset.g || null;
      s.page = 1;
      rerender();
    });
  });
}
