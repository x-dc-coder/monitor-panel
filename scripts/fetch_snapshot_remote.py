#!/usr/bin/env python3
"""在 cloud（dc-sy）上执行：抓取全部真实数据源 → /tmp/monitor-snapshots/*.json
供本地 MONITOR_MOCK=snapshot 模式读取（静态真实数据，本机测试）。
纯标准库：urllib / subprocess / socket / os / json / time。

输出格式 = app.collectors.base.CollectorResult.to_snapshot()。
"""
import json
import os
import re
import socket
import statistics
import subprocess
import time
import urllib.request

OUT = "/tmp/monitor-snapshots"
os.makedirs(OUT, exist_ok=True)
TS = time.time()
NODE = "cloud-1"


def _load_env() -> None:
    """凭据收敛（issue #1）：与 app/config.py 同一搜索顺序加载 env 文件。

    本脚本在 cloud 上经 `cat remote.py | ssh python3 -` 执行（fetch_snapshot.sh），
    非交互 shell 不继承 .env，故此处显式加载：
    MONITOR_ENV_FILE > /opt/monitor-panel/env > 脚本同目录 .env。
    """
    import pathlib
    cands = []
    explicit = os.environ.get("MONITOR_ENV_FILE", "").strip()
    if explicit:
        cands.append(pathlib.Path(explicit))
    cands.append(pathlib.Path("/opt/monitor-panel/env"))
    try:
        cands.append(pathlib.Path(__file__).resolve().parent.parent / ".env")
    except NameError:  # 经 `cat | ssh python3 -` stdin 执行时无 __file__
        pass
    for p in cands:
        if not p.is_file():
            continue
        for raw in p.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
        break


_load_env()

# 凭据经 env 文件/环境变量注入（与 app/config.py 同源），不落代码仓库
FRPS_AUTH = (os.environ.get("MONITOR_FRPS_USER", "admin"),
             os.environ.get("MONITOR_FRPS_PASS", ""))   # 取自 /etc/frp/frps.toml
LEGACY_TOKEN = os.environ.get("MONITOR_LEGACY_TOKEN", "")  # 8899 API_TOKEN
CERT_DOMAINS = ["dc-sy.cn", "headscale.dc-sy.cn", "monitor.dc-sy.cn"]


def http_get(url, headers=None, auth=None, timeout=6):
    req = urllib.request.Request(url, headers=headers or {})
    if auth:
        import base64
        raw = ("%s:%s" % auth).encode()
        req.add_header("Authorization", "Basic " + base64.b64encode(raw).decode())
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def run(cmd, timeout=15):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           stdin=subprocess.DEVNULL)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return -1, "", "timeout"


def metric(name, value, ts=TS):
    return {"name": name, "value": float(value), "ts": ts}


# ================= 1) frps dashboard =================
try:
    si = http_get("http://127.0.0.1:7500/api/serverinfo", auth=FRPS_AUTH)
    serverinfo = {
        "version": si.get("version"), "bind_port": si.get("bindPort"),
        "cur_conns": si.get("curConns"), "client_count": si.get("clientCounts"),
        "total_traffic_in": si.get("totalTrafficIn"),
        "total_traffic_out": si.get("totalTrafficOut"),
        "allow_ports": si.get("allowPortsStr"),
        "proxy_type_count": si.get("proxyTypeCount"),
    }
    tunnels = []
    for ptype in ("tcp", "udp", "http", "https", "stcp", "xtcp"):
        try:
            data = http_get("http://127.0.0.1:7500/api/proxy/" + ptype, auth=FRPS_AUTH)
        except Exception:
            continue
        for p in data.get("proxies", []):
            conf = p.get("conf", {})
            tunnels.append({
                "tunnel_id": p.get("name"),
                "type": conf.get("type", p.get("type")),
                "remote_port": conf.get("remotePort"),
                "local_addr": (str(conf.get("localIP", "")) + ":" + str(conf.get("localPort", "")))
                              if conf.get("localIP") else None,
                "status": p.get("status", "unknown"),
                "traffic_in": p.get("todayTrafficIn", 0) or 0,
                "traffic_out": p.get("todayTrafficOut", 0) or 0,
                "cur_conns": p.get("curConns", 0),
                "last_start_time": p.get("lastStartTime"),
            })
    frp_degraded = False
    frp_mode = "per-type"
except Exception as e:  # noqa: BLE001
    serverinfo, tunnels, frp_degraded, frp_mode = None, [], True, "degraded"
    print("frps error:", e)

# ================= 2) 8899 agent（frp 链路质量） =================
link = {}
link_metrics = []
try:
    st = http_get("http://127.0.0.1:8899/api/status",
                  headers={"Authorization": "Bearer " + LEGACY_TOKEN})
    devices = st.get("devices", [])
    for d in devices:
        dev = d.get("device_id")
        if not dev:
            continue
        link[dev] = {
            "latency_ms": d.get("latency_ms"), "packet_loss_percent": d.get("packet_loss_percent"),
            "frpc_alive": d.get("frpc_alive"), "frps_alive": d.get("frps_alive"),
            "bandwidth_up_kbps": d.get("bandwidth_up_kbps"),
            "bandwidth_down_kbps": d.get("bandwidth_down_kbps"),
            "age_seconds": d.get("age_seconds"), "display_status": d.get("display_status"),
            "label": d.get("label"), "cpu_percent": d.get("cpu_percent"),
            "mem_percent": d.get("mem_percent"),
        }
        for src, base in (("latency_ms", "latency_ms"), ("packet_loss_percent", "packet_loss_percent"),
                          ("frpc_alive", "frpc_alive"), ("frps_alive", "frps_alive"),
                          ("bandwidth_up_kbps", "bandwidth_up_kbps"),
                          ("bandwidth_down_kbps", "bandwidth_down_kbps")):
            v = d.get(src)
            if v is not None:
                link_metrics.append(metric(base + "." + dev, v))
except Exception as e:  # noqa: BLE001
    print("8899 error:", e)

frp_metrics = []
if serverinfo:
    for k, base in (("totalTrafficIn", "total_traffic_in"), ("totalTrafficOut", "total_traffic_out")):
        frp_metrics.append(metric(base, serverinfo.get(k) or 0))
    frp_metrics.append(metric("cur_conns", serverinfo.get("cur_conns") or 0))
    frp_metrics.append(metric("client_count", serverinfo.get("client_count") or 0))
frp_metrics.append(metric("proxy_count", len(tunnels)))

json.dump({
    "node_id": NODE, "group": "frp_traffic", "metrics": frp_metrics,
    "groups": {"frp_link": link_metrics},
    "payload": {"serverinfo": serverinfo, "tunnels": tunnels, "link": link,
                "degraded": frp_degraded, "proxy_mode": frp_mode,
                "source": "frps-dashboard", "ts": TS},
}, open(OUT + "/frp.json", "w"), ensure_ascii=False)

# ================= 3) headscale metrics =================
hs_vals = {"tcp_ok": 0.0, "derp_ok": 0.0}
try:
    with urllib.request.urlopen("http://127.0.0.1:9090/metrics", timeout=6) as r:
        text = r.read().decode()
    keymap = {
        "headscale_nodestore_nodes": "nodes_total",
        "headscale_nodestore_queue_depth": "queue_depth",
        "headscale_mapresponse_generated_total": "mapresponse_generated",
        "headscale_mapresponse_sent_total": "mapresponse_sent",
    }
    for line in text.splitlines():
        m = re.match(r"^(headscale_\S+)\s+([\d.eE+-]+)", line)
        if m and m.group(1) in keymap:
            hs_vals[keymap[m.group(1)]] = float(m.group(2))
except Exception as e:  # noqa: BLE001
    print("headscale metrics error:", e)
try:
    s = socket.create_connection(("127.0.0.1", 8080), timeout=3)
    s.close()
    hs_vals["tcp_ok"] = hs_vals["derp_ok"] = 1.0
except Exception:  # noqa: BLE001
    pass
hs_metrics = [metric(k, v) for k, v in hs_vals.items()]
json.dump({
    "node_id": NODE, "group": "headscale", "metrics": hs_metrics,
    "groups": {},
    "payload": {"values": hs_vals, "derp_region": 999, "ts": TS},
}, open(OUT + "/headscale.json", "w"), ensure_ascii=False)

# ================= 4) tailscale status + ping =================
def parse_last_seen(raw):
    if not raw or not re.match(r"^\d{4}-", raw):
        return None
    try:
        from datetime import datetime, timezone
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None

devices, pings, ts_metrics = [], {}, []
try:
    rc, out, err = run(["tailscale", "status", "--json"], timeout=15)
    data = json.loads(out) if rc == 0 else {}
    self_p = data.get("Self", {})
    ips = self_p.get("TailscaleIPs") or []
    devices.append({
        "node_id": NODE, "device_id": self_p.get("ID") or "self",
        "name": self_p.get("HostName") or "self",
        "tailscale_ip": next((i for i in ips if i.startswith("100.64.")), None),
        "os": self_p.get("OS"), "online": 1 if data.get("BackendState") == "Running" else 0,
        "relay": None, "last_seen_ts": None, "rx_bytes": 0, "tx_bytes": 0, "updated_ts": TS,
    })
    for key, p in (data.get("Peer") or {}).items():
        ips = p.get("TailscaleIPs") or []
        v4 = next((i for i in ips if i.startswith("100.64.")), None)
        devices.append({
            "node_id": NODE, "device_id": p.get("ID") or key,
            "name": p.get("HostName") or key, "tailscale_ip": v4,
            "os": p.get("OS"), "online": 1 if p.get("Online") else 0,
            "relay": p.get("Relay") or ("direct" if p.get("CurAddr") else None),
            "last_seen_ts": parse_last_seen(p.get("LastSeen")),
            "rx_bytes": p.get("RxBytes") or 0, "tx_bytes": p.get("TxBytes") or 0,
            "updated_ts": TS,
        })
    # ping 轮巡：每设备 3 包取中位
    for dev in devices[1:]:
        ip = dev["tailscale_ip"]
        if not ip:
            continue
        samples = []
        path = "unknown"
        for _ in range(3):
            rc2, out2, _ = run(["tailscale", "ping", ip], timeout=8)
            m = re.search(r"pong from \S+ \([\d.]+\) via (\S+) in ([\d.]+)ms", out2)
            if rc2 == 0 and m:
                samples.append(float(m.group(2)))
                path = "derp" if "DERP" in m.group(1).upper() else "direct"
        if samples:
            ping = {"device_id": dev["device_id"], "hostname": dev["name"],
                    "tailscale_ip": ip, "latency_ms": round(statistics.median(samples), 1),
                    "latency_min_ms": round(min(samples), 1), "path": path,
                    "loss_percent": round(100.0 * (3 - len(samples)) / 3, 1),
                    "timed_out": False, "ts": TS, "packets": samples}
        else:
            ping = {"device_id": dev["device_id"], "hostname": dev["name"],
                    "tailscale_ip": ip, "latency_ms": None, "path": "unknown",
                    "loss_percent": 100.0, "timed_out": True, "ts": TS, "packets": []}
        pings[dev["device_id"]] = ping
        if ping["latency_ms"] is not None:
            ts_metrics.append(metric("latency_ms." + (dev["name"] or "?"), ping["latency_ms"]))
            ts_metrics.append(metric("packet_loss_percent." + (dev["name"] or "?"), ping["loss_percent"]))
except Exception as e:  # noqa: BLE001
    print("tailscale error:", e)

json.dump({
    "node_id": NODE, "group": "ts_latency", "metrics": ts_metrics, "groups": {},
    "payload": {"devices": devices, "pings": pings,
                "backend_state": data.get("BackendState") if "data" in dir() else None, "ts": TS},
}, open(OUT + "/tailscale.json", "w"), ensure_ascii=False)

# ================= 5) certs =================
certs = []
for domain in CERT_DOMAINS:
    path = "/etc/letsencrypt/live/%s/cert.pem" % domain
    # live/ 目录 700（root only）：os.path.exists 对普通用户为 False，直接 sudo -n 尝试
    rc, out, err = run(["sudo", "-n", "openssl", "x509", "-in", path, "-noout", "-enddate"], timeout=10)
    m = re.search(r"notAfter=(.+)", out) if rc == 0 else None
    if not m:
        continue
    try:
        from datetime import datetime, timezone
        dt = datetime.strptime(m.group(1), "%b %d %H:%M:%S %Y GMT").replace(tzinfo=timezone.utc)
        days = (dt - datetime.now(timezone.utc)).total_seconds() / 86400
    except ValueError:
        continue
    status = "crit" if days < 7 else ("warn" if days < 30 else "ok")
    certs.append({"domain": domain, "days_left": round(days, 1),
                  "not_after": m.group(1), "status": status, "path": path})
cert_metrics = [metric("days_left." + c["domain"], c["days_left"]) for c in certs]
json.dump({
    "node_id": NODE, "group": "cert_days", "metrics": cert_metrics, "groups": {},
    "payload": {"certs": certs, "ts": TS},
}, open(OUT + "/certs.json", "w"), ensure_ascii=False)

# ================= 6) system（cloud 本机） =================
def read_proc(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return ""

load1 = read_proc("/proc/loadavg").split()
load = [float(load1[0]), float(load1[1]), float(load1[2])] if load1 else [0.0, 0.0, 0.0]

mem_total = mem_avail = 0
for line in read_proc("/proc/meminfo").splitlines():
    if line.startswith("MemTotal:"):
        mem_total = int(line.split()[1])
    elif line.startswith("MemAvailable:"):
        mem_avail = int(line.split()[1])
mem_pct = round(100.0 * (mem_total - mem_avail) / mem_total, 1) if mem_total else 0.0

disk = {}
try:
    df = subprocess.run(["df", "-P"], capture_output=True, text=True, timeout=10).stdout
    for line in df.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 6 and parts[5] == "/":
            used = int(parts[2]) * 1024
            total = int(parts[1]) * 1024
            disk["/"] = {"percent": round(100.0 * used / total, 1) if total else 0.0,
                         "total_gb": round(total / 2**30, 1), "used_gb": round(used / 2**30, 1)}
except Exception:  # noqa: BLE001
    pass

def net_counters():
    c = {}
    for line in read_proc("/proc/net/dev").splitlines()[2:]:
        if ":" not in line:
            continue
        iface, rest = line.split(":", 1)
        p = rest.split()
        if len(p) >= 9 and iface.strip() != "lo":
            c[iface.strip()] = (int(p[0]), int(p[8]))
    return c

n1, t1 = net_counters(), time.time()
time.sleep(1)
n2, t2 = net_counters(), time.time()
rx = sum(v[0] for v in n2.values()) - sum(v[0] for v in n1.values())
tx = sum(v[1] for v in n2.values()) - sum(v[1] for v in n1.values())
dt = max(t2 - t1, 0.1)
net = {"rx_kbps": round(rx * 8 / 1024 / dt, 1), "tx_kbps": round(tx * 8 / 1024 / dt, 1)}

cpu_pct = 0.0
try:
    s1 = read_proc("/proc/stat").splitlines()[0].split()[1:]
    time.sleep(1)
    s2 = read_proc("/proc/stat").splitlines()[0].split()[1:]
    idle1, idle2 = int(s1[3]), int(s2[3])
    tot1, tot2 = sum(int(x) for x in s1), sum(int(x) for x in s2)
    cpu_pct = round(100.0 * (1 - (idle2 - idle1) / max(tot2 - tot1, 1)), 1)
except Exception:  # noqa: BLE001
    pass

sys_metrics = [
    metric("cpu_percent", cpu_pct), metric("mem_percent", mem_pct),
    metric("disk_percent", (disk.get("/") or {}).get("percent", 0.0)),
    metric("load_1m", load[0]), metric("load_5m", load[1]), metric("load_15m", load[2]),
    metric("net_rx_kbps", net["rx_kbps"]), metric("net_tx_kbps", net["tx_kbps"]),
]
json.dump({
    "node_id": NODE, "group": "system", "metrics": sys_metrics, "groups": {},
    "payload": {"cpu_percent": cpu_pct, "mem_percent": mem_pct, "disk": disk,
                "load": load, "net": net, "ts": TS},
}, open(OUT + "/system.json", "w"), ensure_ascii=False)

# ================= 7) network probes（cloud 出站视角） =================
def probe_icmp(target):
    rc, out, _ = run(["ping", "-c", "4", "-W", "2", target], timeout=20)
    m = re.search(r"=\s*[\d.]+/([\d.]+)/[\d.]+", out)
    loss_m = re.search(r"([\d.]+)%\s+packet loss", out)
    return {
        "latency_ms": round(float(m.group(1)), 1) if (rc == 0 and m) else None,
        "loss_percent": float(loss_m.group(1)) if loss_m else 0.0,
        "reachable": rc == 0,
    }

def probe_http(url):
    t0 = time.time()
    try:
        with urllib.request.urlopen(url, timeout=8) as r:
            return {"latency_ms": round((time.time() - t0) * 1000, 1),
                    "reachable": r.status < 500, "status_code": r.status, "error": None}
    except Exception as e:  # noqa: BLE001
        return {"latency_ms": None, "reachable": False, "error": str(e)[:80]}

def probe_dns(target):
    t0 = time.time()
    try:
        socket.getaddrinfo(target, 443)
        return {"latency_ms": round((time.time() - t0) * 1000, 1), "reachable": True, "error": None}
    except Exception as e:  # noqa: BLE001
        return {"latency_ms": None, "reachable": False, "error": str(e)[:80]}

def probe_tcp(host, port):
    t0 = time.time()
    try:
        s = socket.create_connection((host, port), timeout=8)
        s.close()
        return {"latency_ms": round((time.time() - t0) * 1000, 1), "reachable": True, "error": None}
    except Exception as e:  # noqa: BLE001
        return {"latency_ms": None, "reachable": False, "error": str(e)[:80]}

probes = []
for p in [
    {"name": "ali-dns", "kind": "icmp", "target": "223.5.5.5"},
    {"name": "cloudflare-dns", "kind": "icmp", "target": "1.1.1.1"},
    {"name": "public-https", "kind": "http", "target": "https://example.com"},
    {"name": "dns-resolve", "kind": "dns", "target": "example.com"},
    {"name": "cloud2-443", "kind": "tcp", "target": "2.2.2.2", "port": 443},
]:
    try:
        if p["kind"] == "icmp":
            r = probe_icmp(p["target"])
        elif p["kind"] == "http":
            r = probe_http(p["target"])
        elif p["kind"] == "dns":
            r = probe_dns(p["target"])
        else:
            r = probe_tcp(p["target"], p.get("port", 443))
    except Exception as e:  # noqa: BLE001
        r = {"latency_ms": None, "reachable": False, "error": str(e)[:80]}
    probes.append(dict(p, **r, ts=TS))

net_metrics = []
for p in probes:
    net_metrics.append(metric("latency_ms." + p["name"], p["latency_ms"] or 0.0))
    net_metrics.append(metric("loss_percent." + p["name"], p.get("loss_percent", 0.0)))
    net_metrics.append(metric("reachable." + p["name"], 1.0 if p["reachable"] else 0.0))

json.dump({
    "node_id": NODE, "group": "network", "metrics": net_metrics, "groups": {},
    "payload": {"probes": probes, "ts": TS},
}, open(OUT + "/network.json", "w"), ensure_ascii=False)

print("snapshots written to", OUT)
for f in sorted(os.listdir(OUT)):
    print(" ", f, os.path.getsize(os.path.join(OUT, f)), "bytes")

