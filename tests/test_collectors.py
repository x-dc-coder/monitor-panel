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


def test_certs_gather_isolates_failure(monkeypatch):
    """单域名 _probe_tls 抛非预期异常（ValueError）不得废掉整轮。"""
    from app import config
    from app.collectors import certs as certs_mod

    async def fake_discover(self):
        return []

    async def fake_read(self, domain):
        return None

    async def fake_probe(self, domain):
        if domain == "bad.example":
            raise ValueError("boom")
        return {"domain": domain, "days_left": 30.0, "status": "ok",
                "source": "tls"}

    monkeypatch.setattr(config, "CERT_DOMAINS", [])
    monkeypatch.setattr(certs_mod, "_CORE_DOMAINS", ["ok.example", "bad.example"])
    monkeypatch.setattr(certs_mod.CertsCollector, "_nginx_discover", fake_discover)
    monkeypatch.setattr(certs_mod.CertsCollector, "_read_cert", fake_read)
    monkeypatch.setattr(certs_mod.CertsCollector, "_probe_tls", fake_probe)

    result = _run(certs_mod.CertsCollector(mock=False)._run_real())
    certs = {c["domain"]: c for c in result.payload["certs"]}
    assert certs["ok.example"]["days_left"] == 30.0
    assert certs["bad.example"].get("error"), "失败域名应走既有探测失败路径"


def test_certs_gather_isolates_child_cancel(monkeypatch):
    """子任务被单独 cancel（CancelledError 为 BaseException）不得废掉整轮。"""
    from app import config
    from app.collectors import certs as certs_mod

    async def fake_discover(self):
        return []

    async def fake_read(self, domain):
        if domain == "bad.example":
            return {"domain": domain, "days_left": 11.0, "status": "warn",
                    "source": "file"}
        return None

    async def fake_probe(self, domain):
        if domain == "bad.example":
            asyncio.get_running_loop().call_soon(asyncio.current_task().cancel)
            await asyncio.sleep(30)
        return {"domain": domain, "days_left": 30.0, "status": "ok",
                "source": "tls"}

    monkeypatch.setattr(config, "CERT_DOMAINS", [])
    monkeypatch.setattr(certs_mod, "_CORE_DOMAINS", ["ok.example", "bad.example"])
    monkeypatch.setattr(certs_mod.CertsCollector, "_nginx_discover", fake_discover)
    monkeypatch.setattr(certs_mod.CertsCollector, "_read_cert", fake_read)
    monkeypatch.setattr(certs_mod.CertsCollector, "_probe_tls", fake_probe)

    result = _run(certs_mod.CertsCollector(mock=False)._run_real())
    certs = {c["domain"]: c for c in result.payload["certs"]}
    assert all(isinstance(c, dict) for c in result.payload["certs"])
    assert certs["ok.example"]["days_left"] == 30.0
    assert certs["ok.example"]["source"] == "tls"
    # 取消被归一化为 None → 触发本地文件回退
    assert certs["bad.example"]["days_left"] == 11.0
    assert certs["bad.example"]["source"] == "file"
    names = {m.name for m in result.metrics}
    assert "days_left.ok.example" in names
    assert "days_left.bad.example" in names


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


# ---------------- services 探测：单次读协议识别（SSH banner / HTTP） ----------------

def _probe_against_mock(handler, monkeypatch):
    """起本地 mock TCP 服务，用真实端口跑一次 ServicesCollector._probe。"""
    from app import config
    from app.collectors.services import ServicesCollector

    monkeypatch.setattr(config, "SERVICES_CONNECT_TIMEOUT", 2.0)
    monkeypatch.setattr(config, "SERVICES_HTTP_TIMEOUT", 2.0)
    col = ServicesCollector(mock=False)
    target = {"device_id": "local", "device_name": "localhost",
              "tailscale_ip": "127.0.0.1"}

    async def scenario():
        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        async with server:
            return await col._probe(target, port)

    return asyncio.run(scenario())


def test_probe_detects_ssh_banner(monkeypatch):
    """sshd 主动 banner 必须被首读识别为 ssh（旧实现二次读永远等不到数据）。"""
    async def handler(reader, writer):
        writer.write(b"SSH-2.0-OpenSSH_9.6\r\n")
        await writer.drain()
        writer.close()

    row = _probe_against_mock(handler, monkeypatch)
    assert row is not None
    assert row["protocol"] == "ssh"
    assert row["port"] > 0


def test_probe_http_single_read(monkeypatch):
    """HTTP 响应仍按首读判定（重构后不能回归）。"""
    async def handler(reader, writer):
        await reader.read(1024)
        writer.write(b"HTTP/1.1 200 OK\r\nServer: mock-srv\r\n"
                     b"Content-Type: text/html\r\n\r\n<title>MOCK</title>")
        await writer.drain()
        writer.close()

    row = _probe_against_mock(handler, monkeypatch)
    assert row is not None
    assert row["protocol"] == "http"
    assert row["http_status"] == 200
