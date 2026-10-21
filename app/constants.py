"""指标白名单枚举（评审 A 遗漏 2 落实）。

metric_name 收敛为常量枚举：samples 表 insert 前经 is_valid_metric() 白名单校验，
防止脏指标名污染趋势查询。支持 "<base>.<suffix>" 形态（如 latency_ms.oneplus13），
suffix 用于同一基础指标的多实例（设备/挂载点/探测对/域名）。
"""
from __future__ import annotations

# ---- metric_group 枚举 ----
GROUP_FRP_TRAFFIC = "frp_traffic"   # frps dashboard serverinfo/proxy 流量
GROUP_FRP_LINK = "frp_link"         # 8899 agent 上报的 frp 链路质量
GROUP_TS_LATENCY = "ts_latency"     # tailscale ping 延迟/丢包
GROUP_SYSTEM = "system"             # 本机资源（psutil）
GROUP_NETWORK = "network"           # 公网探测对
GROUP_CERT_DAYS = "cert_days"       # 证书剩余天数
GROUP_HEADSCALE = "headscale"       # headscale 控制面
GROUP_SERVICES = "services"         # 服务台账（issue #2：端口约定驱动自动发现）
GROUP_GRM = "grm"                   # Release 监控（issue #4：聚合 github-release-monitor）
GROUP_LAB = "lab_monitor"           # 本机实验监控（lab-monitor 转发器上报，issue #11）
GROUP_LISTEN_PORTS = "listen_ports"  # 各机器 agent 自报本机监听端口

METRIC_GROUPS = (
    GROUP_FRP_TRAFFIC,
    GROUP_FRP_LINK,
    GROUP_TS_LATENCY,
    GROUP_SYSTEM,
    GROUP_NETWORK,
    GROUP_CERT_DAYS,
    GROUP_HEADSCALE,
    GROUP_SERVICES,
    GROUP_GRM,
    GROUP_LAB,
    GROUP_LISTEN_PORTS,
)

# ---- 各 group 允许的基础指标名 ----
GROUP_METRICS: dict[str, tuple[str, ...]] = {
    GROUP_FRP_TRAFFIC: (
        "total_traffic_in",   # bytes，原始计数（E17：存原始值，重启归零由前端虚线提示）
        "total_traffic_out",
        "cur_conns",
        "client_count",
        "proxy_count",
    ),
    GROUP_FRP_LINK: (
        "latency_ms",          # 8899 /api/status 实测字段
        "packet_loss_percent",
        "frpc_alive",          # 1/0
        "frps_alive",          # 1/0
        "bandwidth_up_kbps",
        "bandwidth_down_kbps",
    ),
    GROUP_TS_LATENCY: (
        "latency_ms",          # 每设备 3 包中位/最小
        "packet_loss_percent",
        "ping_timeout",        # 1=在线但超时（聚合层防抖计数依据）
    ),
    GROUP_SYSTEM: (
        "cpu_percent",
        "mem_percent",
        "disk_percent",        # 根分区；分区粒度用 disk_percent.<mount>
        "load_1m",
        "load_5m",
        "load_15m",
        "net_rx_kbps",
        "net_tx_kbps",
    ),
    GROUP_NETWORK: (
        "latency_ms",          # latency_ms.<probe_name>
        "loss_percent",
        "reachable",           # 1/0
    ),
    GROUP_CERT_DAYS: (
        "days_left",           # days_left.<domain>；负值=已过期
    ),
    GROUP_HEADSCALE: (
        "nodes_total",
        "queue_depth",
        "mapresponse_generated",
        "mapresponse_sent",
        "tcp_ok",              # 8080 探活 1/0
        "derp_ok",             # 控制面可达性代理 1/0
    ),
    GROUP_SERVICES: (
        "latency_ms",          # latency_ms.<ip:port>
        "reachable",           # reachable.<ip:port> 1/0
    ),
    GROUP_GRM: (
        "repos_total",         # 监控仓库总数
        "repos_with_release",  # 有最新 release 的仓库数
        "repos_new",           # 带"新发布"标记的仓库数
    ),
    GROUP_LAB: (
        "gpu_util_percent",    # GPU 利用率 %（30s 窗口均值，#13-2）
        "gpu_util_max",        # GPU 利用率 %（30s 窗口峰值，#13-2）
        "vram_used_mib",       # 显存已用 MiB
        "vram_total_mib",      # 显存总量 MiB
        "vram_util_percent",   # 显存占用 %
        "gpu_temp_c",          # 温度 °C
        "cpu_percent",         # 整机 CPU 利用率 %
        "mem_percent",         # 整机内存占用 %
        "mem_used_mib",        # 整机内存已用 MiB
        "mem_total_mib",       # 整机内存总量 MiB
        "experiment_active",   # 当前活跃实验数
        "experiments_total",   # 累计归档实验数
        "alerts_crit",         # 未解决 critical 告警数
        "alerts_warn",         # 未解决 warn 告警数
        "monitor_enabled",     # 引擎开关 1/0
    ),
    GROUP_LISTEN_PORTS: (
        "listening_count",     # 本机监听端口数（agent 自报）
    ),
}

# ---- 告警级别 ----
LEVEL_INFO = "info"
LEVEL_WARN = "warn"
LEVEL_CRIT = "crit"
ALERT_LEVELS = (LEVEL_INFO, LEVEL_WARN, LEVEL_CRIT)

# ---- 设备状态（tailscale 在线状态）----
DEVICE_ONLINE = "online"
DEVICE_OFFLINE = "offline"
DEVICE_UNKNOWN = "unknown"


def is_valid_metric(group: str, name: str) -> bool:
    """白名单校验：group 必须在枚举内；name 为基础指标名或 "<base>.<suffix>"。"""
    if group not in GROUP_METRICS:
        return False
    base = name.split(".", 1)[0]
    return base in GROUP_METRICS[group]


def base_name(name: str) -> str:
    return name.split(".", 1)[0]
