"""aggregator 告警/修复逻辑测试（2026-08-15 审查回归）。"""
import pytest

from app import constants, db
from app.collectors.base import CollectorResult


def _frp_link_result(link: dict) -> CollectorResult:
    r = CollectorResult(node_id="cloud-1", group=constants.GROUP_FRP_LINK)
    r.payload = {"link": link}
    return r


def _agg():
    from app.services.aggregator import Aggregator
    return Aggregator()


def test_frp_link_missing_data_no_false_resolve(tmp_path):
    """lat/loss 均为 None（数据缺失）→ 不 alert 也不 resolve（不误判）。"""
    db.close()
    db.init_db(str(tmp_path / "t.db"))
    agg = _agg()
    # 先插入一个未解决告警
    db.insert_alert("warn", "frp_link:dev-a", "旧告警", cooldown_s=0)
    # 数据缺失的一轮
    agg._on_frp_link(_frp_link_result({"dev-a": {"latency_ms": None,
                                                 "packet_loss_percent": None}}), 0.0)
    assert db.count_unresolved_alerts() == 1, "数据缺失不应 resolve 旧告警"
    db.close()


def test_frp_link_alert_and_resolve(tmp_path):
    """超阈值 → 告警；恢复低于阈值 → resolve。"""
    db.close()
    db.init_db(str(tmp_path / "t.db"))
    agg = _agg()
    # 超阈值
    agg._on_frp_link(_frp_link_result(
        {"dev-a": {"latency_ms": 300, "packet_loss_percent": 1.0}}), 0.0)
    assert db.count_unresolved_alerts() >= 1, "超阈值应产生告警"
    # 恢复
    agg._on_frp_link(_frp_link_result(
        {"dev-a": {"latency_ms": 50, "packet_loss_percent": 0.0}}), 1.0)
    assert db.count_unresolved_alerts() == 0, "恢复后应 resolve"
    db.close()
