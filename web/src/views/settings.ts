/** 设置视图：monitor.conf 终端输出 + 数据源状态 + 采集器进程表（healthz 真实数据）。 */
import { S } from "../state";
import { esc, pageHead, st, tb, win } from "../ui";

export function renderSettings(): string {
  const hz = S.healthz;
  const o = S.overview;
  const frp = o ? o.frp : null;
  const hs = o ? o.headscale : null;
  const c = o ? o.certs : null;
  const linkCount = S.frp ? S.frp.link.length : 0;
  const colls = hz ? hz.collectors : {};

  const procRows = Object.entries(colls).map(([name, p]) => {
    const load = Math.min(100, Math.round(((p.last_run_dur_s || 0) / Math.max(p.interval, 1)) * 100 * 4));
    const errCls = (p.errors || 0) > 0 ? ' style="color:var(--crit)"' : "";
    return '<tr><td class="muted">—</td><td>' + esc(name) + "_collector</td>" +
      '<td class="muted">' + (p.interval || "?") + 's</td><td class="num">' + (p.runs || 0) +
      '</td><td class="num">' + (p.skipped || 0) + '</td><td class="num"' + errCls + ">" +
      (p.errors || 0) + '</td><td class="muted">' + (p.last_run_dur_s || 0).toFixed(3) + "s</td>" +
      '<td><span class="ps-bar" style="width:40px"><i style="width:' + load + '%"></i></span>' +
      '<span class="num muted">' + load + "%</span></td></tr>";
  }).join("");

  const cfg = '<span class="sec">[node]</span><br>' +
    '<span class="k">node_id</span> = <span class="v">"' + esc(hz ? hz.node : "cloud-1") + '"</span> ' +
    '<span class="c"># 多云扩展: cloud-2 待接入</span><br>' +
    '<span class="k">role</span> = <span class="v">"primary"</span> · <span class="k">version</span> = <span class="v">' +
    esc(hz ? hz.version : "?") + '</span><br><br>' +
    '<span class="sec">[collect]</span><br>' +
    '<span class="k">frp</span> = 30s · <span class="k">tailscale</span> = 30s · <span class="k">ts_ping</span> = 60s/台<br>' +
    '<span class="k">headscale</span> = 30s · <span class="k">system</span> = 30s · <span class="k">network</span> = 60s · <span class="k">certs</span> = 3600s<br><br>' +
    '<span class="sec">[thresholds]</span><br>' +
    '<span class="k">ts_latency</span> = <span class="v">200ms warn / 500ms crit</span> · <span class="k">packet_loss</span> = <span class="v">2% / 5%</span><br>' +
    '<span class="k">cert</span> = <span class="v">&lt;30d warn / &lt;7d crit</span> · <span class="k">disk</span> = <span class="v">85% / 92%</span><br>' +
    '<span class="k">alert_cooldown</span> = <span class="v">1800s</span><br><br>' +
    '<span class="sec">[sources]</span><br>' +
    '<span class="k">frps_dashboard</span> = <span class="v">http://127.0.0.1:7500</span> <span class="c"># basic auth env 注入</span><br>' +
    '<span class="k">legacy_8899</span> = <span class="v">http://127.0.0.1:8899/api/status</span> <span class="c"># Bearer <span class="mask">••••••••</span></span><br>' +
    '<span class="k">headscale_metrics</span> = <span class="v">http://127.0.0.1:9090/metrics</span> · <span class="k">derp</span> = <span class="v">region 999</span><br>' +
    '<span class="k">letsencrypt</span> = <span class="v">/etc/letsencrypt/live/*/cert.pem</span> <span class="c"># sudoers 白名单 openssl</span>';

  const srcRows =
    '<tr><td>frps dashboard</td><td class="muted">127.0.0.1:7500</td><td>' +
    st(frp && frp.degraded ? "crit" : "ok", frp && frp.degraded ? "降级" : "ok") +
    '</td><td class="muted">' + (frp ? frp.total : 0) + " 隧道 · per-type 端点</td></tr>" +
    '<tr><td>8899 agent</td><td class="muted">127.0.0.1:8899</td><td>' +
    st(frp && frp.total > 0 && !frp.degraded ? "ok" : "off",
      frp && frp.total > 0 && !frp.degraded ? "ok" : "—") +
    '</td><td class="muted">' + linkCount + " 设备</td></tr>" +
    '<tr><td>headscale metrics</td><td class="muted">127.0.0.1:9090</td><td>' +
    st(hs && hs.tcp_ok ? "ok" : "crit", hs && hs.tcp_ok ? "ok" : "DOWN") +
    '</td><td class="muted">region ' + esc(String(hs ? hs.derp_region : "?")) + " · " +
    esc(String(hs ? hs.nodes_total : "?")) + " 节点</td></tr>" +
    '<tr><td>letsencrypt</td><td class="muted">/etc/letsencrypt/live</td><td>' +
    st((c && (c.crit || 0) > 0) ? "crit" : (c && (c.warn || 0) > 0) ? "warn" : "ok",
      (c && (c.crit || 0) > 0) ? "crit" : (c && (c.warn || 0) > 0) ? "warn" : "ok") +
    '</td><td class="muted">' + (c ? c.total : 0) + " 张 · " + (c ? c.warn : 0) + " warn / " +
    (c ? c.crit : 0) + " crit</td></tr>";

  return pageHead("~/console/settings", "设置", "只读配置视图",
    "cat /opt/monitor-panel/current/.env · 敏感项已脱敏") +
    '<div class="subbar"><span class="sep">▸</span> 配置集中 config.py · 敏感项 env 注入（文件 600 · 不入 git）</div>' +
    '<section class="win"><div class="titlebar"><span class="dots"><i></i><i></i><i></i></span>' +
    '<span class="tt">monitor.conf</span><span class="meta">只读 · 600</span></div>' +
    '<div class="cfg">' + cfg + "</div></section>" +
    win("数据源状态 · sources", "最近采集 · 30s",
      tb(["SOURCE", "ENDPOINT", "状态", "备注"], srcRows)) +
    win("采集器进程 · scheduler", "ps 风格 · /api/healthz 实时",
      tb(["PID", "COMMAND", "CYC", "RUNS", "SKIP", "ERR", "DUR", "LOAD"],
        procRows || "", "等待采集器数据"));
}
