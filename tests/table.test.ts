/**
 * DataTable 纯逻辑单测（node --test，经 esbuild 转译后运行）。
 * 覆盖：分页边界 / 筛选 / 聚合 / 值去重 —— 防回归（TDZ、跳页类 bug）。
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { applyFilter, groupCounts, pageRows, uniqueVals } from "../web/src/table";

const rows = [
  { device: "cloud", status: "open", port: 80 },
  { device: "cloud", status: "open", port: 443 },
  { device: "DC_DC", status: "open", port: 3306 },
  { device: "DC_DC", status: "closed", port: 5432 },
  { device: "pad", status: "closed", port: 10020 },
];

const def = {
  cols: [
    { key: "device", label: "设备", filterable: true },
    { key: "status", label: "状态", filterable: true },
    { key: "port", label: "端口" },
  ],
};

test("pageRows: 第 2 页 → 上一页不越界为 0（回归：跳 0 页 bug）", () => {
  const s = { page: 2, pageSize: 2, filters: {}, group: null, search: "" };
  const r1 = pageRows(rows, s);
  assert.equal(r1.pages, 3);
  assert.equal(r1.items.length, 2);
  // 模拟点上一页一次
  s.page -= 1;
  const r2 = pageRows(rows, s);
  assert.equal(s.page, 1, "上一页后应为第 1 页");
  assert.equal(r2.items[0].port, 80);
  // 第 1 页再上一页 → 钳制为 1
  s.page -= 1;
  pageRows(rows, s);
  assert.equal(s.page, 1, "第 1 页不应再减");
  // 末页下一页 → 钳制
  s.page = 3; s.page += 1;
  pageRows(rows, s);
  assert.equal(s.page, 3, "末页不应再增");
});

test("applyFilter: 多列组合筛选（device + status）", () => {
  const s = { page: 1, pageSize: 10, filters: { device: "cloud", status: "open" }, group: null, search: "" };
  const out = applyFilter(rows, def, s);
  assert.equal(out.length, 2);
  assert.ok(out.every((r) => r.device === "cloud" && r.status === "open"));
});

test("applyFilter: groupBy 分组过滤", () => {
  const s = { page: 1, pageSize: 10, filters: {}, group: "closed", search: "" };
  const out = applyFilter(rows, { ...def, groupBy: "status" }, s);
  assert.equal(out.length, 2);
  assert.ok(out.every((r) => r.status === "closed"));
});

test("applyFilter: 搜索命中任意可读列", () => {
  const s = { page: 1, pageSize: 10, filters: {}, group: null, search: "3306" };
  const out = applyFilter(rows, def, s);
  assert.equal(out.length, 1);
  assert.equal(out[0].port, 3306);
});

test("uniqueVals: 去重且保序", () => {
  assert.deepEqual(uniqueVals(rows, "device"), ["cloud", "DC_DC", "pad"]);
  assert.deepEqual(uniqueVals(rows, "status"), ["open", "closed"]);
});

test("groupCounts: 聚合计数", () => {
  const g = groupCounts(rows, "device");
  assert.deepEqual(g, [["cloud", 2], ["DC_DC", 2], ["pad", 1]]);
});
