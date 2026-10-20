/** 轮询防重入高阶函数：在飞时跳过新一轮调用 */
export function guard<T extends unknown[], R>(fn: (...args: T) => Promise<R>): (...args: T) => Promise<R | undefined> {
  let inflight = false;
  return async (...args: T): Promise<R | undefined> => {
    if (inflight) return undefined;
    inflight = true;
    try {
      return await fn(...args);
    } finally {
      inflight = false;
    }
  };
}
