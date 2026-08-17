/** 告警视图：journalctl 风格日志流 → 通用 DataTable（分页 + 级别/状态筛选 + 按级别聚合）。 */
import { fmtTs } from "../fmt";
import { S } from "../state";
import type { AlertRow } from "../types";
import { esc, pageHead, st, win } from "../ui";
import { table, type TableDef } from "../table";

const TABLE_ID = "alerts";

function lvlCell(a: AlertRow): string {
  const lvl = a.level === "crit" ? "crit" : a.level === "warn" ? "warn" : "off";
  return st(lvl, a.level.toUpperCase());
}

function resCell(a: AlertRow): string {
  return a.resolved ? st("ok", "已解决") : st("crit", "未解决");
}

function msgCell(a: AlertRow): string {
  return '<span class="logline ' + (a.resolved ? "resolved" : "") + '" style="display:block">' +
    '<span class="ts">' + fmtTs(a.ts) + '</span><span class="src">' + esc(a.source) +
    "</span><span class=\"msg\">" + esc(a.message) + "</span></span>";
}

const def: TableDef = {
  cols: [
    { key: "level", label: "级别", filterable: true, fmt: lvlCell },
    {
      key: "resolved", label: "状态", filterable: true, fmt: resCell,
      filterMap: { "0": "未解决", "1": "已解决" },
    },
    { key: "source", label: "来源", fmt: (a) => '<span class="muted">' + esc(a.source) + "</span>" },
    { key: "message", label: "消息", fmt: msgCell },
  ],
  groupBy: "level",
  emptyMsg: "暂无告警",
};

export function renderAlerts(): string {
  const alerts = S.alerts ? S.alerts.alerts : [];
  const un = alerts.filter((a) => !a.resolved).length;
  return pageHead("~/console/alerts", "告警", un + " 未解决 · 30min 冷却",
    "journalctl -u monitor-panel --since today | grep -iE 'warn|crit' · 表格支持级别/状态筛选与分页") +
    '<div class="subbar"><span class="muted">冷却 30min · 保留 90 天 · ' +
    alerts.length + " 条记录</span></div>" +
    win("alerts.log", "tail -f · 自动刷新", table(def, alerts, TABLE_ID));
}
