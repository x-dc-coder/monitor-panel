/** 告警视图：journalctl 风格日志流 + 文本过滤。 */
import { fmtTs } from "../fmt";
import { S } from "../state";
import { esc, pageHead, win } from "../ui";

export function renderAlerts(): string {
  const alerts = S.alerts ? S.alerts.alerts : [];
  const un = alerts.filter((a) => !a.resolved).length;
  return pageHead("~/console/alerts", "告警", un + " 未解决 · 30min 冷却",
    "journalctl -u monitor-panel --since today | grep -iE 'warn|crit'") +
    '<div class="subbar">' +
    '<span class="tbtn on" data-f="all">[全部]</span><span class="tbtn" data-f="warn">[WARN]</span>' +
    '<span class="tbtn" data-f="crit">[CRIT]</span><span class="tbtn" data-f="res">[已解决]</span>' +
    '<span class="sep">│</span><span class="muted">冷却 30min · 保留 90 天 · ' + alerts.length + " 条记录</span></div>" +
    win("alerts.log", "tail -f · 自动刷新",
      '<div id="alert-log">' + (alerts.length ? alerts.map((a) =>
        '<div class="logline ' + (a.resolved ? "resolved" : "") + '" data-lvl="' + esc(a.level) +
        '" data-res="' + (a.resolved ? "1" : "0") + '">' +
        '<span class="ts">' + fmtTs(a.ts) + '</span><span class="lvl ' + esc(a.level) + '">[' +
        esc(a.level.toUpperCase()) + ']</span><span class="src">' + esc(a.source) +
        '</span><span class="msg">' + esc(a.message) + "</span></div>").join("")
        : '<div class="empty">暂无告警</div>') + "</div>");
}

/** 绑定告警过滤（视图渲染后调用） */
export function bindAlertFilter(): void {
  document.querySelectorAll(".subbar .tbtn").forEach((b) => {
    b.addEventListener("click", () => {
      document.querySelectorAll(".subbar .tbtn").forEach((x) => x.classList.remove("on"));
      b.classList.add("on");
      const f = (b as HTMLElement).dataset.f || "all";
      document.querySelectorAll("#alert-log .logline").forEach((l) => {
        const el = l as HTMLElement;
        const lvl = el.dataset.lvl || "";
        const res = el.dataset.res === "1";
        let show = true;
        if (f === "warn") show = lvl === "warn";
        if (f === "crit") show = lvl === "crit";
        if (f === "res") show = res;
        el.style.display = show ? "" : "none";
      });
    });
  });
}
