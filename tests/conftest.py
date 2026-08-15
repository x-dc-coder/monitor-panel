"""pytest 全局夹具：mock 模式 + 隔离测试库。

注意：必须在导入 app.* 之前设置环境变量（config 在 import 时读取）。
"""
import os
import tempfile

os.environ["MONITOR_MOCK"] = "1"
os.environ["MONITOR_DEBUG"] = "0"
_TMP = tempfile.mkdtemp(prefix="monitor-test-")
os.environ["MONITOR_DB_PATH"] = os.path.join(_TMP, "test.db")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import db  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture()
def client():
    """带 lifespan 的 TestClient（启动调度器），每个用例独立空库。"""
    tmp = tempfile.mkdtemp(prefix="monitor-test-db-")
    db.close()
    db.init_db(os.path.join(tmp, "test.db"))
    with TestClient(app) as c:
        yield c
    db.close()


@pytest.fixture()
def ingest_all():
    """手动跑一遍全部 mock 采集器并入库（确定性，不等调度器）。

    注意：不在 fixture 内 set_aggregator——TestClient lifespan 会注册自己的
    聚合层；由用例体内 _seed() 在 client 完全启动后覆盖，保证顺序确定。
    """
    import asyncio

    from app.collectors import build_registry
    from app.services.aggregator import Aggregator

    async def _run():
        reg = build_registry()
        agg = Aggregator()
        for c in reg.values():
            r = await c.run_once()
            if r is not None:
                await agg.ingest(r)
        return agg

    return asyncio.run(_run())
