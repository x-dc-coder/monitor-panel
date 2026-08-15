"""mock 采集器测试：产出规范化结果，指标名全部过白名单。"""
import asyncio

import pytest

from app import constants
from app.collectors import build_registry
from app.collectors.base import CollectorResult


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(scope="module")
def registry():
    return build_registry()


@pytest.mark.parametrize("name", ["frp", "tailscale", "headscale", "system", "network", "certs"])
def test_collector_result_valid(registry, name):
    c = registry[name]
    result = _run(c.run_once())
    assert isinstance(result, CollectorResult)
    assert result.node_id == "cloud-1"
    groups = [(result.group, result.metrics)] + list(result.groups.items())
    for group, metrics in groups:
        assert group in constants.METRIC_GROUPS
        for m in metrics:
            if hasattr(m, "name"):
                assert constants.is_valid_metric(group, m.name), f"{group}/{m.name}"
            else:
                assert constants.is_valid_metric(group, m[0]), f"{group}/{m[0]}"
    assert result.payload is not None


def test_frp_mock_has_tunnels_and_link(registry):
    r = _run(registry["frp"].run_once())
    assert len(r.payload["tunnels"]) == 3
    assert all(t["status"] == "online" for t in r.payload["tunnels"])
    assert "link" in r.payload and r.payload["link"]
    assert constants.GROUP_FRP_LINK in r.groups


def test_tailscale_mock_devices_and_pings(registry):
    r = _run(registry["tailscale"].run_once())
    assert len(r.payload["devices"]) == 4
    assert len(r.payload["pings"]) == 3  # 除 self 外 3 台
    for ping in r.payload["pings"].values():
        assert ping["latency_ms"] is not None
        assert ping["path"] in ("direct", "derp")


def test_network_mock_probes(registry):
    r = _run(registry["network"].run_once())
    names = {p["name"] for p in r.payload["probes"]}
    assert "ali-dns" in names and "cloud2-443" in names
    assert all(p["reachable"] for p in r.payload["probes"])


def test_certs_mock_status(registry):
    r = _run(registry["certs"].run_once())
    statuses = {c["status"] for c in r.payload["certs"]}
    assert "warn" in statuses and "crit" in statuses


def test_sanitize_applied_to_status():
    from app.collectors.base import sanitize
    raw = {"Peer": {"a": {"HostName": "x", "node_key": "nk:1"}}}
    out = sanitize(raw)
    assert out["Peer"]["a"]["node_key"] == "<redacted>"
