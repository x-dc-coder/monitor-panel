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

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app import api as api_mod
from app import config, db
from app.api import (auth, devices, frp, grm, history, ingest, overview,
                     services, system, updates)
from app.collectors import build_registry
from app.scheduler import Scheduler
from app.services.aggregator import Aggregator

logging.basicConfig(
    level=logging.DEBUG if config.DEBUG else logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)

_started = time.time()

# 未认证可读的静态产物清单（精确匹配，对应 web/index.html 的 <link>/<script> 引用）；
# 新增静态资产须在此显式登记，后缀式放行会让未认证者读到任意脚本/映射文件
PUBLIC_ASSETS = frozenset({"/dist/app.js", "/style.css", "/favicon.svg"})


@asynccontextmanager
async def lifespan(app: FastAPI):
    # fail-fast：live 模式凭据缺失直接拒启，防止配置丢失静默上线空数据面板（issue #1）
    cfg_errors = config.validate_config()
    if cfg_errors:
        for e in cfg_errors:
            log.error("config: %s", e)
        raise RuntimeError(f"config validation failed: {cfg_errors}")
    log.info("env files loaded: %s", config.ENV_FILES_LOADED or "(none)")

    db.init_db()
    # 历史未解决告警按 source 压缩为最新一条（insert_alert 去重只防新增不清存量）
    deduped = db.dedup_unresolved_alerts()
    if deduped:
        log.info("alerts: deduped %d stacked unresolved row(s)", deduped)
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
        await db.cleanup_async()   # DELETE+checkpoint 走 to_thread，不阻塞事件循环
    sched.add("cleanup", config.CLEANUP_INTERVAL_S, cleanup_job, timeout=30)

    run_task = asyncio.create_task(sched.run())
    app.state.scheduler = sched
    log.info("monitor-panel started (mode=%s, port=%s)", config.mode_label(), config.PORT)
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


@app.middleware("http")
async def auth_guard_middleware(request: Request, call_next):
    # SSO 未开启或未配置 Client ID 时旁路放行（本地开发与单元测试兼容）
    if not config.SSO_ENABLED or not config.CASDOOR_CLIENT_ID:
        return await call_next(request)

    path = request.url.path

    # 白名单端点放行：
    # 1. 认证路由自身：/auth/*
    # 2. 健康检查：/api/healthz（deploy.sh 健康检查强依赖）
    # 3. 监控指标上报：/api/ingest（dc-desktop 等 agent 走独立 Bearer token 校验）
    # 4. 静态资源：精确产物清单 PUBLIC_ASSETS（新增静态资产须显式登记）
    if (
        path.startswith("/auth/")
        or path == "/api/healthz"
        or path == "/api/ingest"
        or path in PUBLIC_ASSETS
    ):
        return await call_next(request)

    user = request.session.get("user")
    if not user:
        if path.startswith("/api/"):
            return JSONResponse(
                status_code=401,
                content={"ok": False, "error": "unauthorized", "ts": time.time()})
        return RedirectResponse(url="/auth/login")

    return await call_next(request)


@app.exception_handler(HTTPException)
async def _http_exc_handler(request: Request, exc: HTTPException):
    """统一错误信封 {ok:false, error:<str>, ts}（状态码沿用 exc.status_code）。"""
    detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
    return JSONResponse(
        status_code=exc.status_code,
        content={"ok": False, "error": detail, "ts": time.time()},
        headers=getattr(exc, "headers", None) or None)


@app.exception_handler(RequestValidationError)
async def _validation_handler(request: Request, exc: RequestValidationError):
    first = exc.errors()[0] if exc.errors() else {}
    loc = ".".join(str(x) for x in first.get("loc", []))
    msg = f"参数校验失败: {loc}: {first.get('msg', '')}".strip()
    return JSONResponse(
        status_code=422,
        content={"ok": False, "error": msg, "ts": time.time()})


# Session 中间件（包裹在最外层，确保 auth_guard_middleware 可以读取 request.session）
app.add_middleware(
    SessionMiddleware,
    secret_key=config.SESSION_SECRET_KEY,
    session_cookie="monitor_session",
    max_age=7 * 24 * 3600,
    same_site="lax",
    https_only=not config.MOCK,
)


# 挂载认证路由（/auth/login, /auth/callback, /auth/logout, /api/me）
app.include_router(auth.router)

for router in (overview.router, devices.router, frp.router, grm.router,
               history.router, system.router, services.router, ingest.router,
               updates.router):
    app.include_router(router, prefix="/api")


@app.get("/api/healthz")
async def healthz():
    sched = getattr(app.state, "scheduler", None)
    return {
        "ok": True,
        "uptime": round(time.time() - _started, 1),
        "version": config.VERSION,
        "mock": config.MOCK,
        "mode": config.mode_label(),   # mock | snapshot | live（deploy.sh 健康检查用）
        "node": config.NODE_ID,
        "collectors": sched.stats() if sched else {},
    }


# 静态面板（Chart.js vendored 本地，出站异常仍可用）
app.mount("/", StaticFiles(directory=str(config.WEB_DIR), html=True), name="web")
