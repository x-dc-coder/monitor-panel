/** 设备视图：通用 DataTable（分页 + 状态/OS 筛选 + 按状态聚合）+ tailscale ping 终端输出交互。 */
import { fmtMs, nowTime } from "../fmt";
import { S } from "../state";
import { api } from "../api";
import type { DeviceRow, PingResult } from "../types";
import { esc, pageHead, st, statusLevel, win } from "../ui";
import { table, type TableDef } from "../table";

const TABLE_ID = "devices";

function statusCell(d: DeviceRow): string {
  const lvl = statusLevel(d.status || (d.online ? "ok" : "off"));
  const txt = d.online ? (d.status === "degraded" ? "degraded" : "online") : "offline";
  return st(lvl, txt);
}

function latCell(d: DeviceRow): string {
  if (d.timed_out) return '<span style="color:var(--warn)">超时</span>';
  return '<span class="num">' + fmtMs(d.latency_ms) + "</span>";
}

function pingCell(d: DeviceRow): string {
  return '<button type="button" class="tbtn ping-btn" data-id="' + esc(d.device_id) +
    '" data-ip="' + esc(d.tailscale_ip) + '" data-n="' + esc(d.name) + '">[ping]</button>';
}

const def: TableDef = {
  cols: [
    { key: "name", label: "HOST" },
    { key: "tailscale_ip", label: "IP", fmt: (d) => '<span class="muted">' + esc(d.tailscale_ip) + "</span>" },
    { key: "os", label: "OS", filterable: true, sec: true, fmt: (d) => esc(d.os || "—") },
    { key: "status", label: "状态", filterable: true, fmt: statusCell },
    {
      key: "relay", label: "路径",
      fmt: (d) => '<span class="dim2">' + (d.relay ? "DERP(" + esc(d.relay) + ")" : "直连") + "</span>",
    },
    { key: "latency_ms", label: "延迟(中位)", fmt: latCell },
    { key: "mapped_8899", label: "8899", sec: true, fmt: (d) => '<span class="muted">' + esc(d.mapped_8899 || "—") + "</span>" },
    { key: "ping", label: "", fmt: pingCell },
  ],
  groupBy: "status",
  emptyMsg: "暂无设备 · 等待采集",
};

export function renderDevices(): string {
  const devs = S.devices ? S.devices.devices : [];
  const online = devs.filter((d) => d.online).length;
  return pageHead("~/console/devices", "设备", online + " 在线",
    "tailscale status --json && tailscale ping <ip> --c 3 · 表格支持状态/OS 筛选与分页") +
    win("设备清单 · devices", devs.length + " 台 · ping 轮巡 60s/台 · Semaphore(2)",
      table(def, devs, TABLE_ID)) +
    (S.pingLog ? '<div class="tout" id="ping-out">' + S.pingLog + '</div>'
      : '<div class="tout" id="ping-out"><div class="ln"><span class="ps">$</span> ' +
        '<span class="out">tailscale ping &lt;ip&gt; — 点击 [ping] 执行<span class="caret"></span></span></div></div>') +
    "</div>";
}

/** 绑定 [ping] 按钮（视图渲染后调用）；429 显示限流提示 */
export function bindPing(): void {
  document.querySelectorAll<HTMLButtonElement>(".ping-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const id = btn.dataset.id || "";
      const ip = btn.dataset.ip || "";
      const n = btn.dataset.n || "";
      const out = document.getElementById("ping-out");
      if (!out) return;
      btn.disabled = true;
      btn.textContent = "…";
      out.classList.add("show");
      const cmdLine = '<div class="ln"><span class="muted">' + nowTime() +
        '</span> <span class="ps">$</span> <span class="out">sudo tailscale ping ' + esc(ip) + "</span></div>";
      S.pingLog = cmdLine;
      out.innerHTML = cmdLine;
      api<PingResult>("/api/devices/" + encodeURIComponent(id) + "/ping", { method: "POST" })
        .then((d) => {
          const okLine = '<div class="ln"><span class="ok2">pong from ' + esc(n) + " (" + esc(ip) +
            ") via " + esc(d.path === "direct" ? "direct" : "DERP(headscale)") + " in " +
            fmtMs(d.latency_ms) + " ✓</span></div>" +
            '<div class="ln"><span class="out">loss ' + (d.loss_percent || 0) + "% · " +
            (d.mock ? "mock 数据" : "实时采集") + "</span></div>";
          out.innerHTML += okLine;
          S.pingLog = (S.pingLog || "") + okLine;
          btn.textContent = fmtMs(d.latency_ms);
        })
        .catch((e: Error) => {
          const errLine = '<div class="ln"><span class="err2">✕ ' + esc(e.message) +
            "（限流 5s/设备）</span></div>";
          out.innerHTML += errLine;
          S.pingLog = (S.pingLog || "") + errLine;
          btn.textContent = "限流";
        })
        .finally(() => {
          setTimeout(() => { btn.textContent = "[ping]"; btn.disabled = false; }, 5000);
        });
    });
  });
}
