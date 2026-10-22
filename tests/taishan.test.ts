/**
 * TaiShan 视图渲染逻辑单测（node --test）。
 * 覆盖：
 * 1. 空态渲染：未接入/未上报时回退友好提示。
 * 2. 候选排行升序：按加权 score 升序（越低越好）排序，并正确展示 KPI 与标注。
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { S } from "../web/src/state";
import { renderTaishan } from "../web/src/views/taishan";
import type { TaishanData } from "../web/src/types";

test("renderTaishan: 空态（S.taishan 为 null）时输出等待接入提示", () => {
  S.taishan = null;
  const html = renderTaishan();
  assert.ok(html.includes("等待接入"), "应包含等待接入标题");
  assert.ok(html.includes("泰山代理探针（dc-pi）尚未上报数据"), "应提示尚未上报");
});

test("renderTaishan: 渲染正常数据并按 score 升序排列候选节点", () => {
  const mockData: TaishanData = {
    active_node: "hk-01",
    state: "healthy",
    summary: "hk-01 三阶段全通",
    qualified_count: 3,
    top_candidates: [
      { name: "jp-02", score: 205.5, base: 55.0, openai: 240.0, gemini: 300.0 },
      { name: "hk-01", score: 182.0, base: 40.0, openai: 220.0, gemini: 260.0 },
      { name: "us-01", score: 310.0, base: 120.0, openai: 350.0, gemini: 400.0 },
    ],
    switched: false,
    reason: "",
    ts: 1700000000,
  };

  S.taishan = mockData;
  const html = renderTaishan();

  // 状态行与活跃节点验证
  assert.ok(html.includes("healthy"), "状态行应显示 healthy");
  assert.ok(html.includes("hk-01"), "状态行应包含活跃节点 hk-01");
  assert.ok(html.includes("加权 ms · 越低越好"), "SCORE 必须标注「加权 ms · 越低越好」");

  // 表格中按 score 升序排序：hk-01 (182) 应出现在 jp-02 (205.5) 之前，jp-02 应在 us-01 之前
  const idxHk = html.indexOf("<b>hk-01</b>");
  const idxJp = html.indexOf("<b>jp-02</b>");
  const idxUs = html.indexOf("<b>us-01</b>");

  assert.ok(idxHk !== -1, "hk-01 必须被渲染");
  assert.ok(idxJp !== -1, "jp-02 必须被渲染");
  assert.ok(idxUs !== -1, "us-01 必须被渲染");
  assert.ok(idxHk < idxJp, "hk-01 (score 182) 应当排在 jp-02 (score 205.5) 前面");
  assert.ok(idxJp < idxUs, "jp-02 (score 205.5) 应当排在 us-01 (score 310) 前面");
});
