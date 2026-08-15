"""脱敏层测试（评审 F4/E6）：公钥/密钥指纹必须被过滤。"""
from app.collectors.base import sanitize


def test_sanitize_redacts_secret_keys():
    raw = {
        "HostName": "oneplus13",
        "OS": "android",
        "Online": True,
        "node_key": "nodekey:abc123",
        "disco_key": "discokey:xyz",
        "machine_key": "mkey:123",
        "PublicKey": "pubkey:ffff",
        "key_fingerprint": "abcd:ef01",
        "pre_auth_key": "pauth:secret",
        "AuthKey": "auth:value",
        "nested": {"Token": "tok", "password": "pwd", "ok": 1},
        "allowed": {"Relay": "headscale", "TailscaleIPs": ["100.64.0.3"]},
    }
    out = sanitize(raw)
    assert out["node_key"] == "<redacted>"
    assert out["disco_key"] == "<redacted>"
    assert out["machine_key"] == "<redacted>"
    assert out["PublicKey"] == "<redacted>"
    assert out["key_fingerprint"] == "<redacted>"
    assert out["pre_auth_key"] == "<redacted>"
    assert out["AuthKey"] == "<redacted>"
    assert out["nested"]["Token"] == "<redacted>"
    assert out["nested"]["password"] == "<redacted>"
    # 非敏感字段原样保留
    assert out["HostName"] == "oneplus13"
    assert out["allowed"]["Relay"] == "headscale"
    assert "secret" not in str(out["allowed"])


def test_sanitize_handles_lists_and_scalars():
    assert sanitize([{"a": 1}, "b", 3]) == [{"a": 1}, "b", 3]
    assert sanitize("plain") == "plain"
