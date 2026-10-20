"""集中配置：端口/周期/阈值/数据源地址/token/探测对/device_map。

- 敏感项一律从环境变量注入（MONITOR_*），env 文件单一权威源
  （不入 git，云端部署后 chmod 600）。
- env 文件搜索顺序（issue #1 统一）：已存在的环境变量（systemd/测试注入）
  > MONITOR_ENV_FILE 显式路径 > /opt/monitor-panel/env（生产权威源，
  systemd EnvironmentFile 同一文件）> BASE_DIR/.env（本地开发回退）。
- 本地开发默认 mock=True（无需 cloud 依赖即可跑通全部 API/UI）。
- 真实模式启动经 validate_config() fail-fast，防止配置丢失静默上线假数据。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
PROD_ENV_FILE = Path("/opt/monitor-panel/env")
ENV_FILE = BASE_DIR / ".env"

# 实际加载过的 env 文件（启动日志输出来源，排障用）
ENV_FILES_LOADED: list[str] = []


def _env_file_candidates() -> list[Path]:
    cands: list[Path] = []
    explicit = os.environ.get("MONITOR_ENV_FILE", "").strip()
    if explicit:
        cands.append(Path(explicit))
    cands.append(PROD_ENV_FILE)
    cands.append(ENV_FILE)
    return cands


def _load_env_file() -> None:
    """极简 .env 解析（KEY=VALUE，支持 # 注释），避免额外依赖。

    逐候选文件加载，setdefault 语义：已存在的环境变量始终优先，
    多文件并存时先到先得（顺序见 _env_file_candidates）。
    """
    for path in _env_file_candidates():
        if not path.is_file():
            continue
        ENV_FILES_LOADED.append(str(path))
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key:
                os.environ.setdefault(key, value)


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def _env_bool(key: str, default: bool) -> bool:
    v = os.environ.get(key)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, default))
    except (TypeError, ValueError):
        return default


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, default))
    except (TypeError, ValueError):
        return default


def _env_json(key: str, default):
    v = os.environ.get(key)
    if v is None:
        return default
    try:
        return json.loads(v)
    except json.JSONDecodeError:
        return default


_load_env_file()

# ================= 服务 =================
HOST = _env("MONITOR_HOST", "127.0.0.1")          # 仅回环监听，nginx 反代
PORT = _env_int("MONITOR_PORT", 8898)
# 数据模式：true=模拟采集（本地开发默认）| false=真实采集 | snapshot=读取 snapshots/ 真实快照
MOCK_MODE = _env("MONITOR_MOCK", "true").strip().lower()
if MOCK_MODE in ("1", "true", "yes", "on"):
    MOCK, SNAPSHOT = True, False
elif MOCK_MODE in ("0", "false", "no", "off"):
    MOCK, SNAPSHOT = False, False
elif MOCK_MODE == "snapshot":
    MOCK, SNAPSHOT = False, True
else:
    MOCK, SNAPSHOT = True, False
SNAPSHOT_DIR = BASE_DIR / "snapshots"             # 静态真实数据快照（本机测试用）
DEBUG = _env_bool("MONITOR_DEBUG", False)
VERSION = "1.3.0"

# ================= 监控节点（多云预留） =================
NODE_ID = _env("MONITOR_NODE_ID", "cloud-1")
DEFAULT_NODES = [
    {"id": "cloud-1", "name": "cloud-1", "role": "primary",
     "public_ip": "124.221.190.237", "enabled": True},
]
NODES = _env_json("MONITOR_NODES", DEFAULT_NODES)

# ================= 数据源（敏感项 env 注入） =================
# frps dashboard（127.0.0.1:7500，basic auth；frps.toml 中的账号密码）
FRPS_URL = _env("MONITOR_FRPS_URL", "http://127.0.0.1:7500")
FRPS_USER = _env("MONITOR_FRPS_USER", "admin")
FRPS_PASS = _env("MONITOR_FRPS_PASS", "")
FRPS_TIMEOUT = _env_float("MONITOR_FRPS_TIMEOUT", 5.0)

# 既有 8899 frp 面板（只读消费；API_TOKEN 经 env 注入）
LEGACY_STATUS_URL = _env("MONITOR_LEGACY_STATUS_URL", "http://127.0.0.1:8899/api/status")
LEGACY_TOKEN = _env("MONITOR_LEGACY_TOKEN", "")

# headscale 控制面
HEADSCALE_METRICS_URL = _env("MONITOR_HEADSCALE_METRICS_URL", "http://127.0.0.1:9090/metrics")
HEADSCALE_TCP_HOST = _env("MONITOR_HEADSCALE_TCP_HOST", "127.0.0.1")
HEADSCALE_TCP_PORT = _env_int("MONITOR_HEADSCALE_TCP_PORT", 8080)
HEADSCALE_DERP_REGION = _env_int("MONITOR_HEADSCALE_DERP_REGION", 999)

# 证书扫描：TLS SNI 主动探测（443 握手，免 root）为主，nginx 动态发现与
# /etc/letsencrypt/live 本地文件（sudoers 白名单 openssl）为辅
CERT_DOMAINS = _env_json("MONITOR_CERT_DOMAINS", [
    "dc-sy.cn",
    "www.dc-sy.cn",
    "monitor.dc-sy.cn",
    "headscale.dc-sy.cn",
    "sso.dc-sy.cn",
    "releases.monitor.dc-sy.cn",
    "spark.dc-sy.cn",
    "cloudreve.dc-sy.cn",
])
LETSENCRYPT_LIVE = Path(_env("MONITOR_LETSENCRYPT_LIVE", "/etc/letsencrypt/live"))

# 探测对（F4 多云兜底雏形；cloud-2 接入后再补对等探测——2026-08-24 审查移除
# 占位对 cloud2-443→2.2.2.2：每轮必超时拖慢 network 采集 ~8s 且前端恒显 1 fail）
DEFAULT_PROBES = [
    {"name": "ali-dns", "kind": "icmp", "target": "223.5.5.5"},
    {"name": "cloudflare-dns", "kind": "icmp", "target": "1.1.1.1"},
    {"name": "public-https", "kind": "http", "target": "https://example.com"},
    {"name": "dns-resolve", "kind": "dns", "target": "example.com"},
]
PROBES = _env_json("MONITOR_PROBES", DEFAULT_PROBES)

# tailscale HostName ↔ 8899 device_id 对照（可选，缺失不阻塞）
DEVICE_MAP = _env_json("MONITOR_DEVICE_MAP", {
    "vm-0-12-ubuntu": "cloud-server",
    "dc-desktop": "desktop",
    "oppo-pad-mini": "laptop",
})

# ================= Release 监控（issue #4：只读聚合 github-release-monitor）=================
# grm（releases.monitor.dc-sy.cn）与本面板同机部署于 cloud；仓库增删/通知渠道等
# 配置一律在 grm 侧完成，本面板只读聚合展示，不提供任何配置入口。
GRM_DATA_PATH = Path(_env("MONITOR_GRM_DATA_PATH",
                          "/opt/github-release-monitor/data/repositories.json"))
GRM_BASE_URL = _env("MONITOR_GRM_BASE_URL", "https://releases.monitor.dc-sy.cn")

# ================= 服务更新多源聚合（Phase 3：留口子设计） =================
# 启用的 UpdateProvider 名称（app/collectors/updates 注册表键）；grm 为首个实现
UPDATE_PROVIDERS = _env_json("MONITOR_UPDATE_PROVIDERS", ["grm"])

# ================= 服务台账（issue #2：端口约定驱动自动发现） =================
# 约定区：自定义项目/网站统一绑定 [lo, hi] 内端口（文档 docs/架构设计/服务台账与端口约定.md），
# 面板自动扫描该区间完成服务发现 + 探活；区间外固定端口走 manual 登记（db source=manual）。
SERVICES_PORT_RANGE = _env_json("MONITOR_SERVICES_PORT_RANGE", [10000, 19999])
SERVICES_ALIASES = _env_json("MONITOR_SERVICES_ALIASES", {      # {"ip:port": "显示名"}
    "127.0.0.1:8000": "Casdoor（SSO）",
    "127.0.0.1:8787": "Spark",
    "127.0.0.1:5212": "Cloudreve",
    "127.0.0.1:9000": "Doge Gateway",
    "127.0.0.1:5432": "PostgreSQL",
})
# 约定区外必探端口（按设备 IP）：{"100.64.0.1": [22, 443]} 或带探测选项的对象形态
#   {"100.64.0.1": {"443": {"host": "monitor.dc-sy.cn", "https": true, "path": "/"}}}
#   host=Host 头/SNI 域名（nginx 虚拟主机按域名路由，IP 探测会 404）；https=TLS 探测；
#   path=请求路径（如 /metrics）；"*"=所有设备，"local"=本机 127.0.0.1
SERVICES_EXTRA_PORTS = _env_json("MONITOR_SERVICES_EXTRA_PORTS", {
    # 云上核心服务（docs/架构设计/服务台账与端口约定.md 台账：约定区外固定端口，local=本机 127.0.0.1）
    "local": [8000, 8787, 5212, 9000, 5432],
})
# 服务公网直达地址：{"ip:port": "https://..."}，面板链接用它；未配置的内网/回环地址仅展示标记
SERVICES_PUBLIC_URLS = _env_json("MONITOR_SERVICES_PUBLIC_URLS", {})
SERVICES_SCAN_BATCH = _env_int("MONITOR_SERVICES_SCAN_BATCH", 256)   # 每轮每设备扫描端口数（分片）
SERVICES_CONNECT_TIMEOUT = _env_float("MONITOR_SERVICES_CONNECT_TIMEOUT", 1.5)
SERVICES_HTTP_TIMEOUT = _env_float("MONITOR_SERVICES_HTTP_TIMEOUT", 2.0)
SERVICES_CONCURRENCY = _env_int("MONITOR_SERVICES_CONCURRENCY", 64)
SERVICES_MISSING_ROUNDS = _env_int("MONITOR_SERVICES_MISSING_ROUNDS", 3)  # 连续 N 轮未开放 → closed
# ---- 行生命周期与告警降噪（2026-08-24 审查：周报 1199 条未解决误报治理） ----
# closed 状态持续超过该秒数的 auto 行被 prune（表里删除 + 告警 resolve）：
# 消灭"历史登记残留"（extra 已移除的端口）与手机 ephemeral 端口的永久误报
SERVICES_PRUNE_CLOSED_S = _env_int("MONITOR_SERVICES_PRUNE_CLOSED_S", 3600)
# 服务自首次发现起不足该秒数即下线 → 不告警（新发现的临时端口防误报）
SERVICES_ALERT_MIN_AGE_S = _env_int("MONITOR_SERVICES_ALERT_MIN_AGE_S", 600)

# ================= 采集周期（秒） =================
INTERVALS = {
    "frp": _env_int("MONITOR_INTERVAL_FRP", 30),
    "tailscale": _env_int("MONITOR_INTERVAL_TAILSCALE", 30),
    "ts_ping": _env_int("MONITOR_INTERVAL_TS_PING", 60),   # ping 轮巡周期（状态 30s 每轮）
    "headscale": _env_int("MONITOR_INTERVAL_HEADSCALE", 30),
    "system": _env_int("MONITOR_INTERVAL_SYSTEM", 30),
    "network": _env_int("MONITOR_INTERVAL_NETWORK", 60),
    "certs": _env_int("MONITOR_INTERVAL_CERTS", 3600),
    "services": _env_int("MONITOR_INTERVAL_SERVICES", 60),
    "grm": _env_int("MONITOR_INTERVAL_GRM", 300),
}
# tailscale ping 语义（评审 F5/E3/E4）
PING_PACKETS = _env_int("MONITOR_PING_PACKETS", 3)          # 每节点 3 包取中位/最小
PING_TIMEOUT = _env_float("MONITOR_PING_TIMEOUT", 8.0)      # 单包超时（秒）
PING_CONCURRENCY = _env_int("MONITOR_PING_CONCURRENCY", 2)  # asyncio.Semaphore 硬限并发
PING_TIMEOUT_DEBOUNCE_ROUNDS = _env_int("MONITOR_PING_DEBOUNCE_ROUNDS", 3)  # 连续 3 轮防抖
MANUAL_PING_RATELIMIT_S = _env_float("MONITOR_MANUAL_PING_RATELIMIT_S", 5.0)
STEP_TIMEOUT = _env_float("MONITOR_STEP_TIMEOUT", 10.0)     # 每步操作强制超时（评审 B）

# ================= 阈值默认（集中在此） =================
THRESHOLDS = {
    "ts_latency_warn_ms": _env_float("MONITOR_THRESHOLD_TS_LATENCY_WARN", 200),
    "ts_latency_crit_ms": _env_float("MONITOR_THRESHOLD_TS_LATENCY_CRIT", 500),
    "packet_loss_warn_pct": _env_float("MONITOR_THRESHOLD_PACKET_LOSS_WARN", 2),
    "packet_loss_crit_pct": _env_float("MONITOR_THRESHOLD_PACKET_LOSS_CRIT", 5),
    "cert_warn_days": _env_int("MONITOR_THRESHOLD_CERT_WARN_DAYS", 30),
    "cert_crit_days": _env_int("MONITOR_THRESHOLD_CERT_CRIT_DAYS", 7),
    "disk_warn_pct": _env_float("MONITOR_THRESHOLD_DISK_WARN", 85),
    "disk_crit_pct": _env_float("MONITOR_THRESHOLD_DISK_CRIT", 92),
    # tailscale 设备密钥（KeyExpiry）预警：<14d warn，<3d crit，已过期 crit
    "ts_key_warn_days": _env_int("MONITOR_THRESHOLD_TS_KEY_WARN_DAYS", 14),
    "ts_key_crit_days": _env_int("MONITOR_THRESHOLD_TS_KEY_CRIT_DAYS", 3),
}
ALERT_COOLDOWN_S = _env_float("MONITOR_ALERT_COOLDOWN_S", 1800)   # 同类告警 30min 冷却
NETWORK_PROBE_ALERT = _env_bool("MONITOR_NETWORK_PROBE_ALERT", False)  # 探测告警默认关

# ================= 数据库 =================
DB_PATH = Path(_env("MONITOR_DB_PATH", str(BASE_DIR / "data" / "monitor.db")))
SAMPLE_RETENTION_DAYS = _env_int("MONITOR_SAMPLE_RETENTION_DAYS", 30)
ALERT_RETENTION_DAYS = _env_int("MONITOR_ALERT_RETENTION_DAYS", 90)
CLEANUP_INTERVAL_S = _env_int("MONITOR_CLEANUP_INTERVAL_S", 3600)   # 每小时清理+checkpoint

# ================= 命令（sudoers 白名单：deploy/sudoers.monitor） =================
# 真实模式默认经 sudo -n 提权；mock 模式不走系统命令
SUDO = _env_bool("MONITOR_SUDO", not MOCK)
TAILSCALE_CMD = _env("MONITOR_TAILSCALE_CMD", "tailscale")
HEADSCALE_CMD = _env("MONITOR_HEADSCALE_CMD", "headscale")
PING_CMD = _env("MONITOR_PING_CMD", "ping")

# ================= ingest（二期预留，默认 404） =================
INGEST_ENABLED = _env_bool("MONITOR_INGEST_ENABLED", False)
INGEST_TOKEN = _env("MONITOR_INGEST_TOKEN", "")
# 设备表残留清理阈值：连续 N 天未出现在 tailscale status 的行由 cleanup 移除
DEVICE_RESIDENCE_DAYS = _env_int("MONITOR_DEVICE_RESIDENCE_DAYS", 7)

# ================= SSO (Casdoor OIDC) =================
SSO_ENABLED = _env_bool("MONITOR_SSO_ENABLED", True)
CASDOOR_CLIENT_ID = _env("CASDOOR_CLIENT_ID", "")
CASDOOR_CLIENT_SECRET = _env("CASDOOR_CLIENT_SECRET", "")
CASDOOR_ISSUER = _env("CASDOOR_ISSUER", "https://sso.dc-sy.cn").rstrip("/")
# 会话 cookie 签名密钥：模块默认值仅本地开发可用，live 模式启动校验拒绝默认值
_DEFAULT_SESSION_SECRET = "monitor-panel-session-secret-key-change-me"
SESSION_SECRET_KEY = _env("MONITOR_SESSION_SECRET", _DEFAULT_SESSION_SECRET)

# ================= 静态资源 =================
WEB_DIR = BASE_DIR / "web"


# ================= 启动校验（issue #1：fail-fast 防静默假数据） =================
def validate_config() -> list[str]:
    """返回配置错误清单（main.py lifespan 启动时调用，非空即拒启）。

    - live（真实采集）模式：数据源凭据缺失 = 致命（否则上线空数据面板）
    - snapshot 模式：数据来自本地快照，无凭据要求
    - mock 模式：本地开发，无任何必需项
    """
    errors: list[str] = []
    if MOCK and not SNAPSHOT:
        return errors
    if SNAPSHOT:
        return errors
    if not FRPS_PASS:
        errors.append("MONITOR_FRPS_PASS 未设置（frps dashboard 密码，live 模式必需）")
    if not LEGACY_TOKEN:
        errors.append("MONITOR_LEGACY_TOKEN 未设置（8899 面板 API token，live 模式必需）")
    # SSO 启用时会话 cookie 可被默认密钥伪造 → 默认值/过短密钥一律拒启
    if SSO_ENABLED and CASDOOR_CLIENT_ID:
        if SESSION_SECRET_KEY == _DEFAULT_SESSION_SECRET:
            errors.append(
                "MONITOR_SESSION_SECRET 未设置或仍为默认值"
                "（可伪造会话 cookie，live 模式必需）")
        if len(SESSION_SECRET_KEY) < 32:
            errors.append(
                "MONITOR_SESSION_SECRET 长度不足 32 字符"
                "（建议 openssl rand -hex 32 生成）")
    # 启用上报接口却无 token 时，空 Bearer 可绕过认证
    if INGEST_ENABLED and not INGEST_TOKEN:
        errors.append(
            "MONITOR_INGEST_TOKEN 未设置"
            "（INGEST_ENABLED=1 时必需，建议 ≥24 字符）")
    return errors


def mode_label() -> str:
    """当前数据模式（healthz/deploy 健康检查共用语义）。"""
    if MOCK:
        return "mock"
    return "snapshot" if SNAPSHOT else "live"
