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

from typing import Generic, TypeVar, Union

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
    ts: float


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
    ts: float


GrmEnvelope = Envelope[GrmApiData]
