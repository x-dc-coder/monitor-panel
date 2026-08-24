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


@pytest.mark.parametrize("name", ["frp", "tailscale", "headscale", "system", "network", "certs", "services", "grm"])
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
    assert "ali-dns" in names and "dns-resolve" in names
    assert "cloud2-443" not in names   # 2026-08-24 审查：占位探测对已移除
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


# ---- 审查修复回归（2026-08-15）：真实 ping 输出解析 / device_id 脱敏 ----

def test_ping_regex_parses_real_output():
    """真实 tailscale ping 输出（t1 B3 实测格式）必须被 _PONG_RE 解析。"""
    from app.collectors.tailscale import _PONG_RE
    cases = [
        # (输出, 期望延迟)
        ("pong from dc-desktop (100.64.0.2) via DERP(headscale) in 33ms", 33.0),
        ("pong from oneplus13 (100.64.0.3) via DERP(headscale) in 86ms", 86.0),
        ("pong from oppo-pad-mini (100.64.0.4) via 223.66.55.189:5243 in 19ms", 19.0),
    ]
    for out, expected in cases:
        m = _PONG_RE.search(out)
        assert m is not None, f"regex failed on: {out}"
        assert float(m.group(4)) == expected, f"latency mismatch on: {out}"


def test_ping_regex_rejects_garbage():
    """不匹配真实格式时不应误解析。"""
    from app.collectors.tailscale import _PONG_RE
    for bad in ["timeout", "pong from x (1.2.3.4) via DERP in abcms", ""]:
        assert _PONG_RE.search(bad) is None, f"should not match: {bad!r}"


def test_device_id_is_hashed_and_stable():
    """device_id 必须脱敏（8 位 hash），且同一设备 hash 稳定、不同设备不同。"""
    from app.collectors.tailscale import _hash_id
    a = _hash_id("n1234567890abcdef")
    b = _hash_id("n1234567890abcdef")
    c = _hash_id("n9876543210fedcba")
    assert a == b and len(a) == 8
    assert a != c
    assert "1234567890" not in a  # 原始 ID 不残留


def test_norm_device_uses_hashed_id():
    """_norm_device 输出的 device_id 必须是 hash 而非原始 ID。"""
    from app.collectors.tailscale import TailscaleCollector
    c = TailscaleCollector(mock=True)
    dev = c._norm_device("key-fallback", {"ID": "nSECRETNODEID123", "HostName": "h1",
                                          "TailscaleIPs": ["100.64.0.9"], "Online": True})
    assert dev["device_id"] != "nSECRETNODEID123"
    assert len(dev["device_id"]) == 8
