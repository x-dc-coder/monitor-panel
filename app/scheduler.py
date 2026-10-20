"""轻量 asyncio 调度器（评审 E13 落实）。

- 最小堆按 next_run（time.monotonic()）排序，asyncio.sleep 驱动
- overrun 跳过：上一轮未完成则跳过本轮（skipped 计数），不追跑不堆积
- jitter：每轮间隔 ±10% 随机抖动，避免多采集器同拍齐射
- 每任务独立超时（默认 2× 间隔，封顶 60s），单任务失败不影响其他
"""
from __future__ import annotations

import asyncio
import heapq
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

log = logging.getLogger(__name__)

JITTER = 0.10  # ±10%


@dataclass
class ScheduledTask:
    name: str
    interval: float
    factory: Callable[[], Awaitable[None]]  # 无参返回协程（每轮新建，避免协程对象复用）
    jitter: float = JITTER
    timeout: float | None = None
    next_run: float = 0.0  # monotonic
    runs: int = 0
    skipped: int = 0
    errors: int = 0
    last_error: str | None = None
    last_run_dur: float = 0.0

    def _next_interval(self) -> float:
        return self.interval * (1.0 + random.uniform(-self.jitter, self.jitter))

    def schedule_next(self, now: float, skip: bool = False) -> None:
        if skip:
            self.skipped += 1
        self.next_run = now + self._next_interval()


class Scheduler:
    """最小堆调度器。用法：

        sched = Scheduler()
        sched.add("frp", 30, lambda: _job(collector))
        await sched.run()          # 常驻
        await sched.stop()         # 停止
    """

    def __init__(self) -> None:
        self._heap: list[tuple[float, int, ScheduledTask]] = []  # (next_run, seq, task)
        self._tasks: dict[str, ScheduledTask] = {}
        self._running: set[str] = set()
        self._active: set[asyncio.Task] = set()  # 在途执行 Task（stop 收尾等待用）
        self._seq = 0
        self._stop = asyncio.Event()

    def add(self, name: str, interval: float, factory: Callable[[], Awaitable[None]],
            timeout: float | None = None, first_delay: float | None = None) -> None:
        task = ScheduledTask(name=name, interval=interval, factory=factory, timeout=timeout)
        # 首轮快速执行（随机错峰，避免启动齐射），之后按 interval 节奏
        if first_delay is None:
            first_delay = random.uniform(0.3, 1.5)
        task.next_run = time.monotonic() + first_delay
        self._tasks[name] = task
        self._seq += 1
        heapq.heappush(self._heap, (task.next_run, self._seq, task))
        log.info("scheduler: +%s every %.0fs", name, interval)

    def get(self, name: str) -> ScheduledTask | None:
        return self._tasks.get(name)

    def stats(self) -> dict:
        return {
            name: {"interval": t.interval, "runs": t.runs, "skipped": t.skipped,
                   "errors": t.errors, "last_error": t.last_error,
                   "last_run_dur_s": round(t.last_run_dur, 3)}
            for name, t in sorted(self._tasks.items())
        }

    async def _run_one(self, task: ScheduledTask) -> None:
        now = time.monotonic()
        if task.name in self._running:
            # overrun：上一轮未完成 → 跳过本轮，不追跑；重新入堆续期
            task.schedule_next(now, skip=True)
            self._seq += 1
            heapq.heappush(self._heap, (task.next_run, self._seq, task))
            return
        self._running.add(task.name)
        current = asyncio.current_task()
        if current is not None:
            self._active.add(current)
        started = time.monotonic()
        try:
            timeout = task.timeout or min(task.interval * 2, 60.0)
            await asyncio.wait_for(task.factory(), timeout=timeout)
        except asyncio.TimeoutError:
            task.errors += 1
            task.last_error = f"timeout > {timeout:.0f}s"
            log.error("scheduler: %s timed out after %.0fs", task.name, timeout)
        except Exception as exc:  # noqa: BLE001 —— 单任务失败不影响调度器
            task.errors += 1
            task.last_error = str(exc)[:200]
            log.exception("scheduler: %s failed", task.name)
        finally:
            self._running.discard(task.name)
            if current is not None:
                self._active.discard(current)
            task.runs += 1
            task.last_run_dur = time.monotonic() - started
            now = time.monotonic()
            # overrun 语义（评审 E13）：本轮实际完成时间越过下一轮 deadline →
            # 被错过的轮次记 skipped，不追跑；next_run 推进到之后。
            # 2026-10-02 修复：原实现 `max(next_deadline, now) + _next_interval()`
            # 把 interval 计了两次（next_deadline 已含一个 interval），导致所有
            # 采集器实测周期恒为 2× 配置值（healthz runs 恰为理论值一半；certs
            # 实际每 2h 而非 1h → 告警检测延迟被无声放大一倍）。
            next_deadline = task.next_run + task._next_interval()
            missed = 0
            while next_deadline < now:
                missed += 1
                next_deadline += task.interval
            if missed:
                task.skipped += missed
            task.next_run = max(next_deadline, now)
            self._seq += 1
            heapq.heappush(self._heap, (task.next_run, self._seq, task))

    async def run(self) -> None:
        """常驻主循环：睡到下一任务到期，执行后重新入堆。"""
        while not self._stop.is_set():
            if not self._heap:
                await self._stop.wait()
                break
            now = time.monotonic()
            next_run, _, task = self._heap[0]
            if now >= next_run:
                heapq.heappop(self._heap)
                if self._stop.is_set():
                    break
                await self._run_one(task)
            else:
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=next_run - now)
                except asyncio.TimeoutError:
                    pass

    async def stop(self) -> None:
        self._stop.set()
        # 等在途任务收尾（最多 5s）；旧实现 gather(sleep(0), timeout=0) 不等待任何任务，
        # 实际靠 main.py 的 cancel 硬停。排除自身，避免 "Task cannot await on itself"。
        active = [t for t in self._active if t is not asyncio.current_task()]
        if active:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*active, return_exceptions=True), timeout=5)
            except asyncio.TimeoutError:
                log.warning("scheduler: stop timed out after 5s, %d task(s) still running",
                            len(active))
        for name in list(self._running):
            log.info("scheduler: %s still running at stop", name)
