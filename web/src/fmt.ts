/** 格式化工具：字节/时间/百分比/毫秒（全 tabular-nums 友好） */

export function fmtBytes(v: number | null | undefined): string {
  if (v == null || isNaN(v)) return "—";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  let x = v;
  while (x >= 1024 && i < u.length - 1) { x /= 1024; i++; }
  return (i ? x.toFixed(1) : x.toFixed(0)) + " " + u[i];
}

export function fmtMs(v: number | null | undefined): string {
  if (v == null || isNaN(v)) return "—";
  return v >= 100 ? Math.round(v) + "ms" : v.toFixed(1) + "ms";
}

export function fmtPct(v: number | null | undefined): string {
  if (v == null || isNaN(v)) return "—";
  return v.toFixed(1) + "%";
}

function pad(n: number): string {
  return String(n).padStart(2, "0");
}

export function fmtTs(ts: number | null | undefined): string {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  return pad(d.getHours()) + ":" + pad(d.getMinutes()) + ":" + pad(d.getSeconds());
}

export function fmtDate(ts: number | null | undefined): string {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  return pad(d.getMonth() + 1) + "-" + pad(d.getDate());
}

export function nowTime(): string {
  const d = new Date();
  return pad(d.getHours()) + ":" + pad(d.getMinutes()) + ":" + pad(d.getSeconds());
}
