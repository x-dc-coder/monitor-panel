// 构建后给 index.html 的资产 URL 打内容哈希戳（cache-busting）：
// 部署后浏览器强制拉取新 bundle，避免旧前端 + 新后端的渲染错乱。
import { createHash } from "node:crypto";
import { readFileSync, writeFileSync } from "node:fs";

const js = readFileSync("web/dist/app.js");
const css = readFileSync("web/style.css");
const jsH = createHash("sha256").update(js).digest("hex").slice(0, 8);
const cssH = createHash("sha256").update(css).digest("hex").slice(0, 8);
let html = readFileSync("web/index.html", "utf8");
html = html.replace(/style\.css(\?v=[a-f0-9]+)?/, `style.css?v=${cssH}`);
html = html.replace(/dist\/app\.js(\?v=[a-f0-9]+)?/, `dist/app.js?v=${jsH}`);
writeFileSync("web/index.html", html);
console.log(`asset stamp: style.css?v=${cssH} app.js?v=${jsH}`);
