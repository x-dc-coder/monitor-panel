/** 设备视图：设备表 + tailscale ping 终端输出交互。 */
import { fmtMs, nowTime } from "../fmt";
import { S } from "../state";
import { api } from "../api";
import type { PingResult } from "../types";
import { esc, pageHead, st, tb, win } from "../ui";

function statusLevel(s: string): "ok" | "warn" | "crit" | "off" {
  return s === "ok" ? "ok" : s === "warn" ? "warn" : s === "crit" ? "crit" : "off";
}

export function renderDevices(): string {
  const devs = S.devices ? S.devices.devices : [];
  const online = devs.filter((d) => d.online).length;
  return pageHead("~/console/devices", "设备", online + " 在线",
    "tailscale status --json && tailscale ping <ip> --c 3") +
    win("设备清单 · devices", devs.length + " 台 · ping 轮巡 60s/台 · Semaphore(2)",
      tb(["HOST", "IP", "OS", "状态", "路径", "延迟(中位)", "8899", ""],
        devs.map((d) =>
          '<tr data-dev="' + esc(d.device_id) + '"><td>' + esc(d.name) +
          '</td><td class="muted">' + esc(d.tailscale_ip) + "</td><td>" + esc(d.os || "—") +
          "</td><td>" + st(statusLevel(d.status || (d.online ? "ok" : "off")),
            d.online ? (d.status === "degraded" ? "degraded" : "online") : "offline") +
          '</td><td class="dim2">' + (d.relay ? "DERP(" + esc(d.relay) + ")" : "直连") +
          '</td><td class="num">' + (d.timed_out
            ? '<span style="color:var(--warn)">超时</span>' : fmtMs(d.latency_ms)) +
          '</td><td class="muted">' + esc(d.mapped_8899 || "—") + "</td>" +
          '<td><button type="button" class="tbtn ping-btn" data-id="' + esc(d.device_id) +
          '" data-ip="' + esc(d.tailscale_ip) + '" data-n="' + esc(d.name) + '">[ping]</button></td></tr>').join(""),
        "暂无设备 · 等待采集")) +
    (S.pingLog ? '<div class="tout" id="ping-out">' + S.pingLog + '</div>'
      : '<div class="tout" id="ping-out"><div class="ln"><span class="ps">$</span> ' +
        '<span class="out">tailscale ping &lt;ip&gt; — 点击 [ping] 执行<span class="caret"></span></span></div></div>') +
    '</div>';
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
