#!/usr/bin/env node
/**
 * issue #6：openapi → TypeScript 类型生成管线。
 *   1. python scripts/export_openapi.py 导出 openapi.json（FastAPI 内存实例，无需起服务）
 *   2. npx openapi-typescript 生成 web/src/types.gen.ts（提交入库）
 *   3. types.ts 高频区块从生成类型 re-export —— API 加字段漏改 → tsc 编译期报错
 *
 * python 解释器选择：优先项目 venv（.venv/bin/python），回退 PATH 上的 python3。
 */
const { execFileSync } = require("node:child_process");
const { existsSync } = require("node:fs");
const path = require("node:path");

const root = path.resolve(__dirname, "..");
const venvPython = path.join(root, ".venv", "bin", "python");
const python = existsSync(venvPython) ? venvPython : "python3";
const tmpOpenapi = path.join(root, ".openapi.json");
const outFile = path.join(root, "web", "src", "types.gen.ts");

function run(cmd, args, opts = {}) {
  execFileSync(cmd, args, { stdio: "inherit", cwd: root, ...opts });
}

run(python, ["scripts/export_openapi.py", tmpOpenapi]);
run("npx", ["--yes", "openapi-typescript", tmpOpenapi, "-o", outFile]);
console.log("✓ web/src/types.gen.ts 已生成（记得提交；types.ts 高频类型应从其 re-export）");
