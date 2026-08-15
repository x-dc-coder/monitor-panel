"""调度器测试：overrun 跳过不追跑、错误隔离、超时 kill。"""
import asyncio

from app.scheduler import Scheduler


async def _run_for(sched: Scheduler, seconds: float):
    task = asyncio.create_task(sched.run())
    await asyncio.sleep(seconds)
    await sched.stop()
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass


def test_overrun_skip():
    """任务耗时 > 周期 → 下一轮跳过（不追跑不堆积）。"""
    sched = Scheduler()
    state = {"runs": 0}

    async def slow_job():
        state["runs"] += 1
        await asyncio.sleep(0.08)  # 大于周期 0.05，小于 timeout 0.1

    sched.add("slow", 0.05, slow_job, timeout=0.1, first_delay=0.01)
    asyncio.run(_run_for(sched, 0.45))
    stats = sched.stats()["slow"]
    assert stats["runs"] >= 1
    assert stats["skipped"] >= 1
    assert stats["errors"] == 0  # 超时未触发，只是跳过


def test_error_isolation():
    """单任务失败不影响其他任务与调度器。"""
    sched = Scheduler()

    async def bad_job():
        raise RuntimeError("boom")

    async def good_job():
        pass

    sched.add("bad", 0.05, bad_job, timeout=0.5, first_delay=0.01)
    sched.add("good", 0.05, good_job, timeout=0.5, first_delay=0.02)
    asyncio.run(_run_for(sched, 0.3))
    assert sched.stats()["bad"]["errors"] >= 1
    assert sched.stats()["good"]["runs"] >= 1


def test_timeout_kills_slow_job():
    """wait_for 超时 → 任务被标记 error，调度器继续跑。"""
    sched = Scheduler()

    async def hanging():
        await asyncio.sleep(10)

    sched.add("hang", 0.05, hanging, timeout=0.05, first_delay=0.01)
    asyncio.run(_run_for(sched, 0.25))
    stats = sched.stats()["hang"]
    assert stats["errors"] >= 1
    assert "timeout" in (stats["last_error"] or "")
