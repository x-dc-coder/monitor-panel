/** fetch 封装：类型化返回 + 错误归一。 */
import type { Envelope } from "./types";

export class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}

export async function api<T>(path: string, opts?: RequestInit): Promise<T> {
  let r: Response;
  try {
    r = await fetch(path, opts);
  } catch (e) {
    throw new ApiError("网络不可达: " + (e instanceof Error ? e.message : String(e)), 0);
  }
  if (r.status === 401) {
    // 未授权或 Session 失效：跳转 SSO 登录
    window.location.href = "/auth/login";
    throw new ApiError("未授权，正在跳转登录...", 401);
  }
  let j: Envelope<T>;
  try {
    j = (await r.json()) as Envelope<T>;
  } catch {
    throw new ApiError("非 JSON 响应 (HTTP " + r.status + ")", r.status);
  }
  if (!r.ok) {
    const detail = (j as unknown as { detail?: string }).detail || j.error || ("HTTP " + r.status);
    throw new ApiError(detail, r.status);
  }
  if (!j.ok) {
    throw new ApiError(j.error || "API 返回 ok=false", r.status);
  }
  return j.data;
}
