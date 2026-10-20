"""OpenAPI schema 模型（issue #6：openapi → TypeScript 类型生成）。

关键设计：模型挂在路由的 responses={200: {"model": ...}} 上——只参与
openapi.json schema 生成，不做运行时响应过滤/校验（response_model 才过滤），
渐进接入零回归风险。

前端管线：`npm run gen:types` → scripts/export_openapi.py 导出 schema →
openapi-typescript 生成 web/src/types.gen.ts（提交入库）→ types.ts 高频
区块从生成类型 re-export。此后 API 加字段漏改前端 → tsc 编译期报错。

渐进路线（issue #6 方案 2）：先覆盖高频/新写的 services 与 grm 两个端点，
其余端点（overview/devices/frp/certs/network/history）在手写 interface
漂移时逐个迁入——迁入 = 此处补 model + 路由挂 responses + types.ts 改 re-export。
"""
from __future__ import annotations

from typing import Any, Generic, TypeVar, Union

from pydantic import BaseModel

T = TypeVar("T")


class Envelope(BaseModel, Generic[T]):
    """统一响应信封：{ok, data, ts}（app/api/__init__.py ok()）。"""
    ok: bool
    data: T
    ts: float


# ================= /api/services =================
class ServiceItem(BaseModel):
    device_id: str
    device_name: str | None
    tailscale_ip: str
    port: int
    name: str | None
    auto_name: str | None
    protocol: str | None
    url: str | None
    public_url: str | None
    http_status: int | None
    latency_ms: float | None
    status: str
    missing_rounds: int | None
    source: str
    enabled: bool
    first_seen_ts: float | None
    last_seen_ts: float | None
    updated_ts: float | None


class ServicesSummary(BaseModel):
    total: int
    open: int
    closed: int
    manual: int


class ServicesData(BaseModel):
    services: list[ServiceItem]
    range: list[int]
    summary: ServicesSummary
    ts: float | None = None


ServicesEnvelope = Envelope[ServicesData]


# ================= /api/grm =================
class GrmLatest(BaseModel):
    tag: str
    name: str | None
    url: str | None
    prerelease: bool
    published_ts: float | None


class GrmRecentCommit(BaseModel):
    """grm fork 扩展字段（B 系列 recentCommits）；结构未冻结，宽松声明。"""
    model_config = {"extra": "allow"}

    sha: str | None = None
    message: str | None = None
    ts: float | None = None


class GrmRepo(BaseModel):
    repo_id: str
    url: str | None
    owner_slug: str | None
    display_name: str | None
    tags: list[str]
    is_pinned: bool
    is_new: bool
    latest: GrmLatest | None
    readme_summary: str | None
    recent_commits: list[GrmRecentCommit]
    first_seen_ts: float | None
    updated_ts: float | None


class GrmApiSummary(BaseModel):
    total: int
    with_release: int
    new_release: int
    degraded: bool
    error: Union[str, None]


class GrmApiData(BaseModel):
    repos: list[GrmRepo]
    summary: GrmApiSummary
    base_url: str
    ts: float | None = None


GrmEnvelope = Envelope[GrmApiData]


# ================= /api/certs =================
class CertSummaryItem(BaseModel):
    domain: str
    days_left: float
    not_after: str | None = None
    not_before: str | None = None
    issuer: str | None = None
    subject: str | None = None
    sans: list[str] = []
    chain_verified: bool | None = None
    source: str | None = None
    status: str
    error: str | None = None
    path: str | None = None


class CertsData(BaseModel):
    certs: list[CertSummaryItem]
    ts: float | None = None


CertsEnvelope = Envelope[CertsData]


# ================= /api/network =================
class ProbeRow(BaseModel):
    name: str
    kind: str
    target: str
    port: int | None = None
    latency_ms: float | None = None
    loss_percent: float | None = None
    reachable: bool | int
    elapsed_ms: float | None = None
    status_code: int | None = None
    error: str | None = None
    ts: float | None = None
    mock: bool | None = None


class NetworkData(BaseModel):
    probes: list[ProbeRow]
    ts: float | None = None


NetworkEnvelope = Envelope[NetworkData]


# ================= /api/system =================
class SystemData(BaseModel):
    cpu_percent: float | None = None
    mem_percent: float | None = None
    disk: dict[str, Any] | None = None
    load: list[float] | None = None
    net: dict[str, float] | None = None
    ts: float | None = None


SystemEnvelope = Envelope[SystemData]


# ================= /api/frp =================
class TunnelRow(BaseModel):
    tunnel_id: str
    type: str | None = None
    remote_port: int | None = None
    local_addr: str | None = None
    status: str | None = None
    traffic_in: float | None = None
    traffic_out: float | None = None
    cur_conns: float | None = None
    last_start_time: str | None = None
    updated_ts: float | None = None


class LinkRow(BaseModel):
    device_id: str
    hostname: str | None = None
    latency_ms: float | None = None
    packet_loss_percent: float | None = None
    frpc_alive: int | None = None
    frps_alive: int | None = None
    bandwidth_up_kbps: float | None = None
    bandwidth_down_kbps: float | None = None
    age_seconds: float | None = None
    display_status: str | None = None


class FrpData(BaseModel):
    tunnels: list[TunnelRow]
    serverinfo: dict[str, Any] | None = None
    link: list[LinkRow]
    degraded: bool | None = None
    proxy_mode: str | None = None
    source: str | None = None
    ts: float | None = None


FrpEnvelope = Envelope[FrpData]


# ================= /api/history =================
class HistoryPoint(BaseModel):
    ts: float
    value: float
    ts_max: float | None = None


class HistoryData(BaseModel):
    metric: str
    group: str | None = None
    node: str | None = None
    range: str
    bucket_s: int
    points: list[HistoryPoint]
    count: int
    since_ts: float | None = None


HistoryEnvelope = Envelope[HistoryData]


# ================= /api/devices =================
class DeviceRow(BaseModel):
    device_id: str
    name: str | None = None
    tailscale_ip: str | None = None
    os: str | None = None
    online: bool
    status: str
    relay: str | None = None
    last_seen_ts: float | None = None
    rx_bytes: int | None = None
    tx_bytes: int | None = None
    updated_ts: float | None = None
    latency_ms: float | None = None
    latency_min_ms: float | None = None
    path: str | None = None
    loss_percent: float | None = None
    timed_out: bool
    ping_ts: float | None = None
    mapped_8899: str | None = None
    key_expiry_ts: float | None = None
    key_days_left: float | None = None
    key_status: str
    dns_name: str | None = None
    allowed_ips: list[str]
    subnet_routes: list[str]
    exit_node: bool
    exit_node_option: bool


class DevicesData(BaseModel):
    devices: list[DeviceRow]
    device_map: dict[str, str]


DevicesEnvelope = Envelope[DevicesData]


class PingData(BaseModel):
    device_id: str
    hostname: str | None = None
    tailscale_ip: str | None = None
    latency_ms: float | None = None
    latency_min_ms: float | None = None
    path: str | None = None
    loss_percent: float | None = None
    timed_out: bool
    ts: float | None = None
    packets: list[float] | None = None
    mock: bool | None = None


PingEnvelope = Envelope[PingData]


# ================= /api/alerts =================
class AlertRow(BaseModel):
    id: int
    ts: float
    level: str
    source: str
    message: str
    resolved: int


class AlertsData(BaseModel):
    alerts: list[AlertRow]


AlertsEnvelope = Envelope[AlertsData]


# ================= /api/updates =================
class UpdateItemRow(BaseModel):
    provider: str
    item_key: str
    title: str | None = None
    url: str | None = None
    version: str | None = None
    version_url: str | None = None
    is_new: bool
    prerelease: bool
    tags: list[str]
    is_pinned: bool
    summary: str | None = None
    published_ts: float | None = None
    updated_ts: float | None = None


class UpdatesData(BaseModel):
    items: list[UpdateItemRow]
    providers: list[str]
    summary: dict[str, Any]
    ts: float | None = None


UpdatesEnvelope = Envelope[UpdatesData]


# ================= /api/overview =================
class NodeInfo(BaseModel):
    id: str
    role: str | None = None
    last_seen_ts: float | None = None


class FrpOverview(BaseModel):
    total: int
    online: int
    traffic_in: float | None = None
    traffic_out: float | None = None
    degraded: bool
    proxy_mode: str | None = None
    error: str | None = None


class TailscaleOverview(BaseModel):
    total: int
    online: int
    backend_state: str | None = None
    any_derp: bool


class SystemOverview(BaseModel):
    cpu_percent: float | None = None
    mem_percent: float | None = None
    load: list[float] | None = None
    net: dict[str, float] | None = None
    disk: dict[str, Any] | None = None


class HeadscaleOverview(BaseModel):
    tcp_ok: float | None = None
    nodes_total: float | None = None
    derp_region: float | None = None


class CertsOverview(BaseModel):
    total: int
    warn: int
    crit: int
    expiring: list[CertSummaryItem]


class NetworkOverview(BaseModel):
    total: int
    ok: int
    fail: int


class GrmOverview(BaseModel):
    total: int
    with_release: int
    new_release: int
    degraded: bool
    error: str | None = None
    base_url: str | None = None


class AlertsOverview(BaseModel):
    unresolved: int


class OverviewData(BaseModel):
    mock: bool
    mode: str
    updated_at: float | None = None
    frp: FrpOverview
    tailscale: TailscaleOverview
    system: SystemOverview
    headscale: HeadscaleOverview
    certs: CertsOverview
    network: NetworkOverview
    grm: GrmOverview
    # lab 区块为 camelCase passthrough，字段治理延后（已知债务）
    lab: dict[str, Any] | None = None
    alerts: AlertsOverview
    nodes: list[NodeInfo]


OverviewEnvelope = Envelope[OverviewData]


# ================= /api/me =================
class MeData(BaseModel):
    id: str | None = None
    name: str | None = None
    email: str | None = None
    avatar: str | None = None


MeEnvelope = Envelope[MeData]
