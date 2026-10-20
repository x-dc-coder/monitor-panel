"""Phase 1~4 整改回归：证书 SNI 探测 / tailscale 扩展字段 / updates 多源 / 隐患修复。"""
import asyncio
import json

import pytest

from app import config, constants, db
from app.collectors import build_registry


def _run(coro):
    return asyncio.run(coro)


def _ingest(agg, result):
    _run(agg.ingest(result))


# ---------------- Phase 1：证书扫描 ----------------

def test_cert_domains_default_covers_all_subdomains():
    want = {"dc-sy.cn", "www.dc-sy.cn", "monitor.dc-sy.cn", "headscale.dc-sy.cn",
            "sso.dc-sy.cn", "releases.monitor.dc-sy.cn", "spark.dc-sy.cn",
            "cloudreve.dc-sy.cn"}
    assert want <= set(config.CERT_DOMAINS)


def test_certs_tls_parse_der_self_signed():
    """_parse_der：x509 解析 SAN/Issuer/not_before/not_after/剩余天数。"""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography import hazmat
    from cryptography.x509.oid import NameOID
    import datetime
    from app.collectors.certs import CertsCollector

    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "dc-sy.cn")]))
            .issuer_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test-ca")]))
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now)
            .not_valid_after(now + datetime.timedelta(days=20))
            .add_extension(x509.SubjectAlternativeName(
                [x509.DNSName("dc-sy.cn"), x509.DNSName("*.dc-sy.cn")]), False)
            .sign(key, hashes.SHA256()))
    out = CertsCollector._parse_der("dc-sy.cn", cert.public_bytes(
        hazmat.primitives.serialization.Encoding.DER), True)
    assert out["status"] == "warn"          # 20 天 < 30 天 warn 阈值
    assert "dc-sy.cn" in out["sans"] and "*.dc-sy.cn" in out["sans"]
    assert "test-ca" in out["issuer"]
    assert out["chain_verified"] is True and out["source"] == "tls"
    assert 19 <= out["days_left"] <= 20.5


def test_certs_aggregator_alert_lifecycle(client):
    """探测失败/已过期 → crit 告警；恢复 → resolve。"""
    from app.collectors.base import CollectorResult
    from app.services.aggregator import Aggregator

    agg = Aggregator()
    r = CollectorResult(node_id="cloud-1", group=constants.GROUP_CERT_DAYS)
    r.payload = {"certs": [{"domain": "x.dc-sy.cn", "days_left": -1.0,
                            "status": "crit"},
                           {"domain": "y.dc-sy.cn", "days_left": 99.0,
                            "status": "ok"}]}
    _ingest(agg, r)
    assert any(a["source"] == "cert:x.dc-sy.cn" and not a["resolved"]
               for a in db.get_alerts())
    assert not any(a["source"] == "cert:y.dc-sy.cn" for a in db.get_alerts())
    # 恢复
    r2 = CollectorResult(node_id="cloud-1", group=constants.GROUP_CERT_DAYS)
    r2.payload = {"certs": [{"domain": "x.dc-sy.cn", "days_left": 99.0,
                             "status": "ok"}]}
    _ingest(agg, r2)
    assert all(a["resolved"] for a in db.get_alerts()
               if a["source"] == "cert:x.dc-sy.cn")


# ---------------- Phase 2：tailscale ----------------

def test_pick_ts_ipv4_full_cgnat_range():
    from app.collectors.tailscale import _pick_ts_ipv4
    assert _pick_ts_ipv4(["100.127.255.9", "8.8.8.8"]) == "100.127.255.9"
    assert _pick_ts_ipv4(["fd7a::1", "100.65.3.4"]) == "100.65.3.4"
    assert _pick_ts_ipv4(["fd7a::1", "192.168.1.9"]) == "192.168.1.9"
    assert _pick_ts_ipv4(["fd7a::1"]) == "fd7a::1"
    assert _pick_ts_ipv4([]) is None


def test_subnet_routes_filters_host_and_cgnat():
    from app.collectors.tailscale import _subnet_routes
    out = _subnet_routes(["100.64.0.1/32", "10.1.0.0/16", "100.64.0.0/10"],
                         ["192.168.1.0/24"], "100.64.0.1")
    assert out == ["10.1.0.0/16", "192.168.1.0/24"]


def test_norm_device_key_expiry_and_fields():
    from app.collectors.tailscale import TailscaleCollector
    import time as _t
    c = TailscaleCollector(mock=True)
    expiry = _t.time() + 2 * 86400
    from datetime import datetime, timezone
    raw = datetime.fromtimestamp(expiry, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    dev = c._norm_device("k", {
        "ID": "nSECRET", "HostName": "h", "TailscaleIPs": ["100.66.0.9"],
        "Online": True, "KeyExpiry": raw, "DNSName": "h.tail-net.ts.net.",
        "AllowedIPs": ["100.66.0.9/32", "10.0.0.0/8"],
        "PrimaryRoutes": ["172.16.0.0/12"], "ExitNodeOption": True})
    assert dev["key_days_left"] is not None and dev["key_days_left"] < 3
    assert dev["dns_name"] == "h.tail-net.ts.net"
    assert dev["subnet_routes"] == ["10.0.0.0/8", "172.16.0.0/12"]
    assert dev["allowed_ips"] == ["100.66.0.9/32", "10.0.0.0/8"]
    assert dev["exit_node_option"] is True and dev["exit_node"] is False


def test_key_expiry_alert_thresholds(client):
    from app.collectors.base import CollectorResult
    from app.services.aggregator import Aggregator

    agg = Aggregator()
    r = CollectorResult(node_id="cloud-1", group=constants.GROUP_TS_LATENCY)
    r.payload = {"devices": [
        {"device_id": "a", "name": "dev-a", "online": 1,
         "key_days_left": 10.0},                       # warn
        {"device_id": "b", "name": "dev-b", "online": 1,
         "key_days_left": 1.0},                        # crit
        {"device_id": "c", "name": "dev-c", "online": 1,
         "key_days_left": 100.0},                      # ok
    ], "pings": {}}
    _ingest(agg, r)
    srcs = {a["source"]: a for a in db.get_alerts() if a["source"].startswith("ts_key:")}
    assert srcs["ts_key:dev-a"]["level"] == "warn"
    assert srcs["ts_key:dev-b"]["level"] == "crit"
    assert "ts_key:dev-c" not in srcs


def test_devices_api_extended_fields(client, ingest_all):
    from app.api import set_aggregator
    set_aggregator(ingest_all)
    r = client.get("/api/devices")
    assert r.status_code == 200
    for d in r.json()["data"]["devices"]:
        for k in ("key_expiry_ts", "key_days_left", "key_status", "dns_name",
                  "allowed_ips", "subnet_routes", "exit_node",
                  "exit_node_option"):
            assert k in d


def test_devices_table_new_columns_roundtrip(client):
    db.upsert_devices("cloud-1", [{
        "device_id": "dev-x", "name": "x", "online": 1,
        "key_expiry_ts": 1893456000.0, "dns_name": "x.ts.net",
        "allowed_ips": ["100.64.0.9/32"], "subnet_routes": ["10.0.0.0/8"],
        "exit_node": True, "exit_node_option": True, "updated_ts": 1.0}])
    row = [d for d in db.get_devices() if d["device_id"] == "dev-x"][0]
    assert row["dns_name"] == "x.ts.net"
    assert json.loads(row["allowed_ips"]) == ["100.64.0.9/32"]
    assert json.loads(row["subnet_routes"]) == ["10.0.0.0/8"]
    assert row["exit_node"] == 1 and row["exit_node_option"] == 1
    assert row["key_expiry_ts"] == 1893456000.0


# ---------------- Phase 3：updates 多源 ----------------

def test_update_items_roundtrip(client):
    db.upsert_update_items("cloud-1", [
        {"provider": "grm", "item_key": "github:a/b", "title": "a/b",
         "version": "v1", "is_new": 1, "tags": '["ops"]'},
        {"provider": "grm", "item_key": "github:c/d", "title": "c/d",
         "version": None},
    ], prune=True)
    rows = db.get_update_items(provider="grm")
    assert len(rows) == 2
    by_key = {r["item_key"]: r for r in rows}
    assert by_key["github:a/b"]["version"] == "v1"
    assert by_key["github:a/b"]["is_new"] == 1
    # prune：未出现的行删除
    db.upsert_update_items("cloud-1", [
        {"provider": "grm", "item_key": "github:a/b", "version": "v2"}],
        prune=True)
    rows = db.get_update_items()
    assert [r["item_key"] for r in rows] == ["github:a/b"]
    assert rows[0]["version"] == "v2"


def test_updates_api(client, ingest_all):
    from app.api import set_aggregator
    set_aggregator(ingest_all)
    r = client.get("/api/updates")
    assert r.status_code == 200
    d = r.json()["data"]
    assert "grm" in d["providers"]
    assert d["summary"]["total"] == len(d["items"])
    for it in d["items"]:
        assert it["provider"] == "grm"
        assert isinstance(it["tags"], list)
    r2 = client.get("/api/updates", params={"provider": "grm"})
    assert r2.status_code == 200
    assert len(r2.json()["data"]["items"]) == d["summary"]["total"]


def test_updates_api_pins_and_summary(client):
    """P1-4：/api/updates 从 grm_repos 读侧合并 is_pinned/summary（不加列）。"""
    db.upsert_grm_repos("cloud-1", [
        {"repo_id": "github:pinned/a", "owner_slug": "pinned/a",
         "is_pinned": 1, "readme_summary": "置顶仓库摘要"},
    ])
    db.upsert_update_items("cloud-1", [
        {"provider": "grm", "item_key": "github:pinned/a", "title": "pinned/a"},
        {"provider": "grm", "item_key": "github:plain/b", "title": "plain/b"},
    ])
    r = client.get("/api/updates")
    assert r.status_code == 200
    by_key = {it["item_key"]: it for it in r.json()["data"]["items"]}
    assert by_key["github:pinned/a"]["is_pinned"] is True
    assert by_key["github:pinned/a"]["summary"] == "置顶仓库摘要"
    # 无对应 grm 行 → False/None
    assert by_key["github:plain/b"]["is_pinned"] is False
    assert by_key["github:plain/b"]["summary"] is None


def test_grm_provider_fetch_and_rows_to_items():
    from app.collectors.updates import GrmProvider, PROVIDERS, get_provider
    from app.collectors.updates.grm import rows_to_items
    assert isinstance(PROVIDERS["grm"], GrmProvider)
    assert get_provider("grm") is PROVIDERS["grm"]
    items = _run(PROVIDERS["grm"].fetch())
    assert items and all(i.provider == "grm" for i in items)
    assert any(i.version for i in items)            # mock 有 latest release
    rows = rows_to_items("cloud-1", [
        {"repo_id": "github:a/b", "owner_slug": "a/b", "latest_tag": "v1",
         "is_new": 1}], 1.0)
    assert rows[0]["item_key"] == "github:a/b" and rows[0]["version"] == "v1"


# ---------------- Phase 4：隐患修复 ----------------

def test_run_cmd_timeout_kills_process_group():
    """超时必须杀整个进程组（孙进程不遗留），并抛 CollectorError。"""
    import sys
    from app.collectors.base import CollectorError, run_cmd
    # sh -c 'sleep 100 & sleep 100'：shell 派生孙进程，killpg 才能清干净
    with pytest.raises(CollectorError):
        _run(run_cmd(["sh", "-c", "sleep 100 & sleep 100"], timeout=0.5))
    import subprocess
    out = subprocess.run(["pgrep", "-f", "sleep 100"], capture_output=True,
                         text=True)
    assert not out.stdout.strip(), f"leaked: {out.stdout!r}"


def test_extra_ports_default_covers_cloud_services():
    assert set(config.SERVICES_EXTRA_PORTS.get("local", [])) >= {
        8000, 8787, 5212, 9000, 5432}
    assert config.SERVICES_ALIASES["127.0.0.1:8000"] == "Casdoor（SSO）"
    assert config.SERVICES_ALIASES["127.0.0.1:5432"] == "PostgreSQL"


def test_cleanup_async_runs_in_thread():
    from app import db as _db
    assert _run(_db.cleanup_async()) is not None
