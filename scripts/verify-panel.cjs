#!/usr/bin/env node
/** 面板端到端验证（Playwright headless，2026-08-22 issue 清零批次沉淀）。
 *
 * 覆盖：Basic Auth 登录 / 全视图遍历 + console 基线 / Release 监控卡片（#4）/
 *       390px 无横向溢出 + 次要列隐藏（#3）/ 768px 复验 / 桌面回归截图。
 *
 * 用法：
 *   PANEL_USER=monitor PANEL_PASS=*** npm run verify:e2e
 *   PANEL_USER=monitor PANEL_PASS=*** PANEL_URL=https://monitor.dc-sy.cn node scripts/verify-panel.cjs
 *
 * 依赖：playwright 库（项目 devDeps 无——依次探测全局 @playwright/mcp 内嵌副本或 NODE_PATH），
 *       chromium 二进制（~/.cache/ms-playwright，自动探测最新 headless shell）。
 * 截图输出：/tmp/panel-shots/（6 张：desktop-overview/releases + m390/m768 两视口）。
 */
"use strict";
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

// ---------- 配置 ----------
const BASE = process.env.PANEL_URL || "https://monitor.dc-sy.cn";
const USER = process.env.PANEL_USER;
const PASS = process.env.PANEL_PASS;
if (!USER || !PASS) {
  console.error("缺少凭据：请设置 PANEL_USER / PANEL_PASS 环境变量（勿硬编码提交）");
  process.exit(2);
}
const SHOT = "/tmp/panel-shots";
const VIEWS = ["overview", "devices", "tunnels", "links", "services",
  "releases", "probes", "certs", "alerts", "settings"];

// ---------- playwright 解析（项目无 playwright devDep，探测常见安装位） ----------
function resolvePlaywright() {
  const candidates = [];
  if (process.env.PLAYWRIGHT_REQ) candidates.push(process.env.PLAYWRIGHT_REQ);
  const roots = ["/usr/lib/node_modules", "/usr/local/lib/node_modules"];
  const nvmBase = path.join(os.homedir(), ".nvm", "versions", "node");
  if (fs.existsSync(nvmBase)) {
    for (const v of fs.readdirSync(nvmBase)) roots.push(path.join(nvmBase, v, "lib", "node_modules"));
  }
  for (const base of roots) {
    if (!fs.existsSync(base)) continue;
    candidates.push(path.join(base, "@playwright", "mcp", "node_modules", "playwright"));
    candidates.push(path.join(base, "playwright"));
  }
  for (const c of candidates) {
    try { return require(c); } catch { /* 下一个候选 */ }
  }
  throw new Error("未找到 playwright 库：npm i -D playwright 或设置 PLAYWRIGHT_REQ 指向其路径");
}

/** 探测 ~/.cache/ms-playwright 下最新的 chromium headless shell / chromium */
function resolveChromium() {
  const cache = path.join(os.homedir(), ".cache", "ms-playwright");
  if (!fs.existsSync(cache)) return undefined;
  const dirs = fs.readdirSync(cache)
    .filter((d) => /^chromium(_headless_shell)?-\d+$/.test(d))
    .sort((a, b) => parseInt(b.split("-").pop()) - parseInt(a.split("-").pop()));
  for (const d of dirs) {
    const shell = path.join(cache, d, "chrome-headless-shell-linux64", "chrome-headless-shell");
    const full = path.join(cache, d, "chrome-linux", "chrome");
    for (const p of [shell, full]) if (fs.existsSync(p)) return p;
  }
  return undefined;
}

(async () => {
  const { chromium } = resolvePlaywright();
  const launchOpts = { headless: true };
  const exec = resolveChromium();
  if (exec) launchOpts.executablePath = exec;
  const browser = await chromium.launch(launchOpts);

  const results = { views: [], consoleErrors: [], mobile: {}, shots: [] };
  fs.mkdirSync(SHOT, { recursive: true });

  // ---------- ① 桌面 1440x900：全视图遍历 + console 基线 ----------
  const ctx = await browser.newContext({
    viewport: { width: 1440, height: 900 },
    httpCredentials: { username: USER, password: PASS },
  });
  const page = await ctx.newPage();
  page.on("console", (m) => { if (m.type() === "error") results.consoleErrors.push(m.text().slice(0, 160)); });
  // /api/me 未登录按规范返回 401 信封（e5008e2 起）；mock fail-open 模式无会话必现，
  // 浏览器把 4xx 响应记为资源错误属预期噪声——按实际发生的 /api/me 401 数量在门禁处抵扣
  let me401Count = 0;
  page.on("response", (r) => { if (r.status() === 401 && r.url().includes("/api/me")) me401Count += 1; });
  page.on("pageerror", (e) => results.consoleErrors.push("PAGEERROR: " + String(e).slice(0, 160)));

  await page.goto(BASE + "/", { waitUntil: "domcontentloaded" });
  await page.waitForTimeout(2500);
  results.title = await page.title();
  results.authOk = await page.locator(".nav .logo .t1").count() > 0;

  for (const v of VIEWS) {
    await page.goto(BASE + "/#/" + v, { waitUntil: "domcontentloaded" });
    await page.waitForTimeout(1200);
    const overflow = await page.evaluate(() =>
      document.documentElement.scrollWidth - document.documentElement.clientWidth);
    const h1 = await page.locator("h1").first().textContent().catch(() => "");
    results.views.push({ view: v, h1: (h1 || "").trim().slice(0, 40), desktopOverflowPx: overflow });
  }

  // Release 监控专项（#4）
  await page.goto(BASE + "/#/releases", { waitUntil: "domcontentloaded" });
  await page.waitForTimeout(1500);
  results.releases = await page.evaluate(() => ({
    cards: document.querySelectorAll(".rm-card").length,
    releaseLinks: [...document.querySelectorAll(".rm-rel .lnk")].slice(0, 3).map((a) => a.textContent.trim()),
    noReleaseCards: [...document.querySelectorAll(".rm-card")]
      .filter((c) => c.textContent.includes("无 release")).length,
    degraded: document.body.textContent.includes("Release 监控暂不可用"),
  }));
  // overview grm 摘要（#4 验收）
  await page.goto(BASE + "/#/overview", { waitUntil: "domcontentloaded" });
  await page.waitForTimeout(1500);
  results.overviewGrm = await page.evaluate(() => {
    const hit = [...document.querySelectorAll(".win")]
      .find((w) => (w.querySelector(".tt") || {}).textContent?.includes("Release"));
    return hit ? hit.textContent.slice(0, 120) : null;
  });
  await page.screenshot({ path: SHOT + "/desktop-overview.png" });
  await page.goto(BASE + "/#/releases", { waitUntil: "domcontentloaded" });
  await page.waitForTimeout(1200);
  await page.screenshot({ path: SHOT + "/desktop-releases.png" });
  await ctx.close();

  // ---------- ② 移动视口（#3 验收：390/768 无横向溢出 + 次要列隐藏） ----------
  for (const vp of [{ w: 390, h: 844, tag: "m390" }, { w: 768, h: 1024, tag: "m768" }]) {
    const mctx = await browser.newContext({
      viewport: { width: vp.w, height: vp.h },
      httpCredentials: { username: USER, password: PASS },
    });
    const mp = await mctx.newPage();
    const per = {};
    for (const v of ["overview", "devices", "services", "releases", "alerts"]) {
      await mp.goto(BASE + "/#/" + v, { waitUntil: "domcontentloaded" });
      await mp.waitForTimeout(1000);
      per[v] = await mp.evaluate(() => ({
        overflowX: document.documentElement.scrollWidth - document.documentElement.clientWidth,
        secColsHidden: document.querySelectorAll("th.col-sec").length > 0 &&
          [...document.querySelectorAll("th.col-sec")].every((th) => getComputedStyle(th).display === "none"),
        navWidth: getComputedStyle(document.querySelector(".nav")).width,
      }));
    }
    for (const v of ["releases", "overview"]) {
      await mp.goto(BASE + "/#/" + v, { waitUntil: "domcontentloaded" });
      await mp.waitForTimeout(800);
      await mp.screenshot({ path: `${SHOT}/${vp.tag}-${v}.png` });
    }
    results.mobile[vp.tag] = per;
    await mctx.close();
  }

  results.shots = fs.readdirSync(SHOT);
  await browser.close();

  // ---------- 判定 ----------
  const fail = [];
  if (!results.authOk) fail.push("Basic Auth 登录失败");
  // 抵扣 /api/me 401 的预期资源错误（数量对齐，防误伤其他 401）
  while (me401Count > 0) {
    const i = results.consoleErrors.findIndex((t) => t.includes("status of 401"));
    if (i < 0) break;
    results.consoleErrors.splice(i, 1);
    me401Count -= 1;
  }
  if (results.consoleErrors.length) fail.push("console errors: " + results.consoleErrors.length);
  for (const v of results.views) if (v.desktopOverflowPx > 0) fail.push(`桌面 ${v.view} 横向溢出 ${v.desktopOverflowPx}px`);
  if (!results.releases.cards) fail.push("Release 监控无卡片（#4）");
  if (!results.overviewGrm) fail.push("overview 缺 grm 摘要行（#4）");
  for (const tag of ["m390", "m768"]) {
    for (const [v, r] of Object.entries(results.mobile[tag])) {
      if (r.overflowX > 0) fail.push(`${tag} ${v} 横向溢出 ${r.overflowX}px（#3）`);
    }
  }
  if (!results.mobile.m390.devices.secColsHidden) fail.push("390px devices 次要列未隐藏（#3）");

  console.log(JSON.stringify(results, null, 1));
  console.log(fail.length ? "✗ FAIL: " + fail.join("; ") : "✓ ALL PASS");
  process.exit(fail.length ? 1 : 0);
})().catch((e) => { console.error("FATAL", e); process.exit(1); });
