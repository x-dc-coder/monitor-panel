"""FastAPI 入口：路由挂载、lifespan 启停调度器、/api/healthz、静态面板。

- 服务仅监听 127.0.0.1（config.HOST/PORT），nginx 反代 443
- lifespan：init_db → 采集器注册表 → 聚合层 → 调度器（每个采集器一个间隔任务）
- 单 worker 强制规范：采集全部 async + 每步超时；psutil 等同步调用 to_thread
"""
from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app import api as api_mod
from app import config, db
from app.api import devices, frp, history, ingest, overview, system
from app.collectors import build_registry
from app.scheduler import Scheduler
from app.services.aggregator import Aggregator

logging.basicConfig(
    level=logging.DEBUG if config.DEBUG else logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)

_started = time.time()


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    registry = build_registry()
    agg = Aggregator()
    api_mod.set_aggregator(agg)

    sched = Scheduler()

    async def collector_job(c) -> None:
        try:
            result = await c.run_once()
            if result is not None:
                await agg.ingest(result)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 —— 单采集器失败不影响调度
            log.exception("collector %s failed", c.name)

    for name, c in registry.items():
        sched.add(name, c.interval, lambda c=c: collector_job(c))

    async def cleanup_job() -> None:
        db.cleanup()
    sched.add("cleanup", config.CLEANUP_INTERVAL_S, cleanup_job, timeout=30)

    run_task = asyncio.create_task(sched.run())
    app.state.scheduler = sched
    log.info("monitor-panel started (mock=%s, port=%s)", config.MOCK, config.PORT)
    try:
        yield
    finally:
        log.info("shutting down scheduler...")
        await sched.stop()
        run_task.cancel()
        try:
            await run_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        db.close()


app = FastAPI(title="monitor-panel", version=config.VERSION, lifespan=lifespan)

for router in (overview.router, devices.router, frp.router,
               history.router, system.router, ingest.router):
    app.include_router(router, prefix="/api")


@app.get("/api/healthz")
async def healthz():
    sched = getattr(app.state, "scheduler", None)
    return {
        "ok": True,
        "uptime": round(time.time() - _started, 1),
        "version": config.VERSION,
        "mock": config.MOCK,
        "node": config.NODE_ID,
        "collectors": sched.stats() if sched else {},
    }


# 静态面板（Chart.js vendored 本地，出站异常仍可用）
app.mount("/", StaticFiles(directory=str(config.WEB_DIR), html=True), name="web")
