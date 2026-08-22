"""Release 监控（issue #4）测试：采集器 mock/降级 / db upsert+prune / 聚合持久化 / API 契约。"""
import asyncio
import json

import pytest

from app import constants, db
from app.collectors import build_registry
from app.collectors.base import CollectorResult


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(scope="module")
def registry():
    return build_registry()


def test_grm_mock_result(registry):
    """mock：确定性仓库清单，含 有release/新发布/无release 三态 + 指标过白名单。"""
    r = _run(registry["grm"].run_once())
    assert isinstance(r, CollectorResult)
    assert r.group == constants.GROUP_GRM
    payload = r.payload
    assert payload["mock"] is True
    repos = payload["repos"]
    assert len(repos) == 3
    slugs = {x["owner_slug"] for x in repos}
    assert "x-dc-coder/monitor-panel" in slugs
    assert payload["summary"]["total"] == 3
    assert payload["summary"]["with_release"] == 2
    assert payload["summary"]["new_release"] == 1
    # 无 release 仓库：latest 字段为空但行保留（前端降级占位）
    norel = [x for x in repos if not x["latest_tag"]]
    assert len(norel) == 1 and norel[0]["readme_summary"] is None
    for m in r.metrics:
        assert constants.is_valid_metric(r.group, m.name), f"{r.group}/{m.name}"


def test_grm_owner_slug_extraction():
    """owner/repo 提取：id 优先，url 回退。"""
    from app.collectors.grm import _owner_slug
    assert _owner_slug({"id": "github:a/b"}) == "a/b"
    assert _owner_slug({"id": "gitlab:self:host/g/h", "url": "https://x/g/h"}) == "g/h"
    assert _owner_slug({"url": "https://github.com/c/d/"}) == "c/d"
    assert _owner_slug({}) == ""


def test_grm_real_reads_file(tmp_path, monkeypatch):
    """real：读 repositories.json（grm Repository[] 结构）→ 规范化。"""
    from app import config
    from app.collectors.grm import GrmCollector
    data_file = tmp_path / "repositories.json"
    data_file.write_text(json.dumps([
        {"id": "github:a/b", "url": "https://github.com/a/b", "tags": ["x"],
         "isNew": True,
         "latestRelease": {"tag_name": "v1", "html_url": "https://h",
                           "published_at": "2026-08-20T12:00:00Z"}},
        {"id": "github:c/d", "url": "https://github.com/c/d"},
    ]), encoding="utf-8")
    monkeypatch.setattr(config, "GRM_DATA_PATH", data_file)
    col = GrmCollector(mock=False)
    r = _run(col._run_real())
    assert r.payload.get("degraded") is not True
    assert r.payload["summary"]["total"] == 2
    assert r.payload["summary"]["with_release"] == 1
    assert r.payload["summary"]["new_release"] == 1
    row = r.payload["repos"][0]
    assert row["owner_slug"] == "a/b" and row["latest_tag"] == "v1"
    assert row["published_ts"] is not None
    assert json.loads(row["tags"]) == ["x"]


def test_grm_degraded_when_file_missing(tmp_path, monkeypatch):
    """降级：文件缺失 → degraded=true + error，正常返回不抛异常。"""
    from app import config
    from app.collectors.grm import GrmCollector
    monkeypatch.setattr(config, "GRM_DATA_PATH", tmp_path / "nope.json")
    r = _run(GrmCollector(mock=False)._run_real())
    assert r.payload["degraded"] is True
    assert "不存在" in r.payload["error"]
    assert r.payload["repos"] == []


def test_grm_degraded_on_bad_json(tmp_path, monkeypatch):
    """降级：JSON 损坏/结构异常 → degraded，不崩。"""
    from app import config
    from app.collectors.grm import GrmCollector
    bad = tmp_path / "bad.json"
    bad.write_text("{not-json", encoding="utf-8")
    monkeypatch.setattr(config, "GRM_DATA_PATH", bad)
    r = _run(GrmCollector(mock=False)._run_real())
    assert r.payload["degraded"] is True

    shape = tmp_path / "shape.json"
    shape.write_text('{"repositories": []}', encoding="utf-8")
    monkeypatch.setattr(config, "GRM_DATA_PATH", shape)
    r2 = _run(GrmCollector(mock=False)._run_real())
    assert r2.payload["degraded"] is True
    assert "结构异常" in r2.payload["error"]


def test_grm_db_upsert_and_prune(tmp_path):
    """upsert 语义：UNIQUE 合并、first_seen 保留、grm 侧删除的仓库被 prune。"""
    db.close()
    db.init_db(str(tmp_path / "t.db"))
    ts1, ts2 = 1000.0, 2000.0
    db.upsert_grm_repos("cloud-1", [
        {"repo_id": "github:a/b", "owner_slug": "a/b", "latest_tag": "v1",
         "first_seen_ts": ts1, "updated_ts": ts1},
        {"repo_id": "github:c/d", "owner_slug": "c/d", "latest_tag": None,
         "first_seen_ts": ts1, "updated_ts": ts1},
    ])
    db.upsert_grm_repos("cloud-1", [
        {"repo_id": "github:a/b", "owner_slug": "a/b", "latest_tag": "v2",
         "first_seen_ts": ts2, "updated_ts": ts2},
    ])
    rows = db.get_grm_repos("cloud-1")
    assert len(rows) == 1                      # c/d 已在 grm 侧删除 → prune
    assert rows[0]["repo_id"] == "github:a/b"
    assert rows[0]["latest_tag"] == "v2"
    assert rows[0]["first_seen_ts"] == ts1     # 首见时间保留
    assert rows[0]["updated_ts"] == ts2
    db.close()


def test_grm_db_degraded_keeps_rows(tmp_path):
    """degraded 轮次：聚合层跳过 upsert（prune 不触发），快照保留。"""
    db.close()
    db.init_db(str(tmp_path / "t.db"))
    from app.services.aggregator import Aggregator
    agg = Aggregator()
    _run(agg.ingest(CollectorResult(
        node_id="cloud-1", group=constants.GROUP_GRM,
        payload={"repos": [{"repo_id": "github:a/b", "owner_slug": "a/b"}]})))
    assert len(db.get_grm_repos()) == 1
    _run(agg.ingest(CollectorResult(
        node_id="cloud-1", group=constants.GROUP_GRM,
        payload={"repos": [], "degraded": True, "error": "x"})))
    assert len(db.get_grm_repos()) == 1        # 降级不清空
    db.close()


def test_grm_api_contract(client, ingest_all):
    """/api/grm 契约：信封 + 仓库卡片字段 + summary + base_url。"""
    from app.api import set_aggregator
    set_aggregator(ingest_all)
    r = client.get("/api/grm")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and "ts" in body and "data" in body
    data = body["data"]
    assert data["summary"]["total"] == 3
    assert data["summary"]["with_release"] == 2
    assert data["summary"]["degraded"] is False
    assert data["base_url"].startswith("https://")
    repo = data["repos"][0]
    for key in ("repo_id", "owner_slug", "tags", "is_pinned", "is_new",
                "latest", "readme_summary", "recent_commits"):
        assert key in repo
    assert isinstance(repo["tags"], list)


def test_grm_overview_block(client, ingest_all):
    """overview 聚合含 grm 区块（issue #4 验收：总览可见）。"""
    from app.api import set_aggregator
    set_aggregator(ingest_all)
    r = client.get("/api/overview")
    assert r.status_code == 200
    grm = r.json()["data"]["grm"]
    assert grm["total"] == 3
    assert grm["with_release"] == 2
    assert grm["new_release"] == 1
    assert grm["degraded"] is False
    assert grm["base_url"].startswith("https://")
