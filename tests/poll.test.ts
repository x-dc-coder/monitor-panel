/**
 * 轮询与 API 健壮性测试（node --test）。
 * 覆盖：
 * 1. guard: 并发调用期间二次调用被跳过、完成后可再次调用
 * 2. appendNodeParam: nodeId 参数拼装
 * 3. api 超时转换: fetch 抛 AbortError 时转化为 ApiError "请求超时"
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { guard } from "../web/src/guard";
import { api, appendNodeParam, ApiError } from "../web/src/api";
import { S, setNode } from "../web/src/state";

test("guard: 并发调用期间二次调用被跳过，完成后可再次调用", async () => {
  let running = 0;
  let calls = 0;
  let resolveFn: () => void = () => {};

  const fn = async () => {
    calls++;
    running++;
    await new Promise<void>((resolve) => {
      resolveFn = resolve;
    });
    running--;
    return "done";
  };

  const guarded = guard(fn);

  // 第一次调用，处于在飞状态
  const p1 = guarded();
  assert.equal(calls, 1);
  assert.equal(running, 1);

  // 第二次并发调用，在飞状态下应被跳过返回 undefined
  const p2 = guarded();
  assert.equal(calls, 1, "在飞期间二次调用应被跳过");
  const res2 = await p2;
  assert.equal(res2, undefined);

  // 结束第一次调用
  resolveFn();
  const res1 = await p1;
  assert.equal(res1, "done");
  assert.equal(running, 0);

  // 完成后再次调用，能够正常执行
  let resolveFn2: () => void = () => {};
  const fn2Promise = guarded();
  assert.equal(calls, 2, "完成前序调用后，再次调用应当执行");
  resolveFn();
  await fn2Promise;
});

test("appendNodeParam: 正确拼接 nodeId 参数（? 与 &）", () => {
  assert.equal(appendNodeParam("/api/overview", null), "/api/overview");
  assert.equal(appendNodeParam("/api/overview", "cloud-1"), "/api/overview?node=cloud-1");
  assert.equal(appendNodeParam("/api/history?metric=cpu", "cloud-1"), "/api/history?metric=cpu&node=cloud-1");
  assert.equal(appendNodeParam("/api/test", "node/with spaces&special"), "/api/test?node=node%2Fwith%20spaces%26special");
});

test("setNode: 切换全局 nodeId 状态", () => {
  setNode("cloud-2");
  assert.equal(S.nodeId, "cloud-2");
  setNode(null);
  assert.equal(S.nodeId, null);
});

test("api 超时转换: fetch 抛 AbortError 时转化为 ApiError('请求超时', 0)", async () => {
  const origFetch = globalThis.fetch;
  try {
    globalThis.fetch = async () => {
      const err = new Error("This operation was aborted");
      err.name = "AbortError";
      throw err;
    };

    await assert.rejects(
      async () => {
        await api("/api/timeout-test");
      },
      (err: unknown) => {
        assert.ok(err instanceof ApiError);
        assert.equal(err.message, "请求超时");
        assert.equal(err.status, 0);
        return true;
      }
    );
  } finally {
    globalThis.fetch = origFetch;
  }
});
