# monitor.dc-sy.cn 统一监控面板 — 开发计划与任务书

> 版本：v1.3 ｜ 更新：2026-08-22 ｜ 状态：**M1–M5 完成 + issue 清零，线上运行中（release 20260822-195935）**
> 本文档是开发工作区的主指引；详细设计见 docs/ 下六份团队报告。

## 开发状态（2026-08-22 更新）

| 里程碑 | 状态 | 说明 |
|---|---|---|
| M1 骨架 | ✅ 完成 | 脚手架、config（env 注入）/constants（指标白名单）/db（WAL+PRAGMA）/scheduler（overrun+jitter+单调时钟）、/api/healthz、mock 采集器、单页框架 |
| M2 数据源接入 | ✅ 完成 | frp（per-type 端点 + /api/proxy/all 探测 + 降级 8899）、tailscale（status+ping 轮巡，Semaphore(2)+超时 kill+3 包中位）、system（psutil+to_thread）、脱敏层 sanitize()；sudoers 白名单已备（deploy/） |
| M3 面板完善 | ✅ 完成 | overview 聚合、设备表（device_map 对照）、隧道表、链路质量表、趋势图（降采样）、证书/headscale/网络探测、告警流、手动 ping（限流 5s/设备） |
| M4 部署上线 | ✅ 完成 | 2026-08-17 上线（scripts/deploy.sh 原子部署：备份→releases→软链→健康检查→自动回滚） |
| M5 加固扩展 | ✅ 完成 | Basic Auth、配置统一（fail-fast + mode 健康检查）、Release 监控聚合、移动端适配、ingest 认证就绪（cloud-2 接入即开）、openapi 类型管线 |

**issue 清零批次（2026-08-22，全 5 个 open issue 落地）**：
- **#1 配置统一**：env 单一权威源 `/opt/monitor-panel/env`（systemd 与 config.py 双通道同源，release 零拷贝）；`validate_config()` live 凭据缺失拒启；deploy.sh 健康检查校验 `mode ∈ {live,snapshot}`（回落 mock = 部署失败自动回滚）；backup-db/fetch_snapshot 散落点收敛；.env.example 覆盖全部 63 变量
- **#4 Release 监控区块**：`collectors/grm.py` 只读聚合同机 repositories.json（5min），grm_repos 快照表 + prune；前端卡片视图（NEW/prerelease/置顶/tags，无 release 降级占位待 fork 扩展字段）+ overview 摘要行；grm 不可读仅本区块降级
- **#5 ingest 认证**：nginx `location = /api/ingest auth_basic off`（limit_except POST）+ 应用层 `hmac.compare_digest` Bearer token；INGEST_ENABLED 保持 false，cloud-2 接入时 env 置 1 即用
- **#6 openapi → TS**：`app/api/schemas.py` + `npm run gen:types` → `web/src/types.gen.ts`（提交），services/grm 高频类型 re-export 生成类型（后端加字段漏改 → tsc 编译期报错）；CI 加类型漂移检查
- **#3 移动端响应式**：768/640/400px 三档媒体查询（侧栏图标栏、DataTable 次要列 sec 隐藏、表格容器横滚、KPI/网格降列）；桌面零回归
- 线上验证：pytest 57 ✓ / npm 6 ✓ / 10 视图 0 console error ✓ / 390px+768px 0 横向溢出 ✓ / Playwright 截图核验 ✓

**二期增强（2026-08-17）**：
- 服务台账（issue #2）：约定区自动发现 + 探活 + 域名探测 + 直达分级 + 下线告警（docs/05）
- 通用 DataTable 组件：分页/筛选/聚合，服务/告警/设备三视图复用（docs/06）
- 技术栈审查整改：面板 Basic Auth、前端 node 单测、GitHub Actions CI、移除 ECharts 死重

**本地开发**：

```bash
cd /home/dc/projects/monitor-panel
uv venv .venv && source .venv/bin/activate
uv pip install -r requirements.txt          # fastapi uvicorn httpx psutil
uvicorn app.main:app --port 8898            # 本地预览（collectors mock=True）
# 冒烟测试（后端）
uv pip install -r requirements-dev.txt && python -m pytest tests/ -q
# 前端（类型检查 + 单测 + 构建）
npm install && npm run typecheck && npm test && npm run build
```

**M4 部署入口**（已上线，日常更新同命令）：

```bash
./scripts/deploy.sh --host dc-sy --remote-dir /opt/monitor-panel   # 原子部署
ssh dc-sy "sudo install -m 440 deploy/sudoers.monitor /etc/sudoers.d/monitor-panel"
ssh dc-sy "sudo install -m 644 deploy/logrotate.conf /etc/logrotate.d/monitor-panel"
ssh dc-sy "sudo cp deploy/backup-db.{service,timer} /etc/systemd/system/ && sudo systemctl enable --now backup-db.timer"
./scripts/setup_nginx_cert.sh --issuance dns   # 或 http（预检 8a 判定）
```

---

## 0. 项目概述

在 cloud（124.221.190.237）部署 `monitor.dc-sy.cn` 统一链路监控面板：
整合 **frp 隧道监控 + tailscale 设备/连接质量 + 云服务器健康**，为"第二台云服务器兜底 / 双公网 IP 互检"预留扩展。

**技术栈**：Python FastAPI + uvicorn（单进程）+ SQLite(WAL) + TypeScript + esbuild（无框架，字符串渲染）
**端口**：8898（仅绑 127.0.0.1，nginx 反代 443）
**访问**：https://monitor.dc-sy.cn（**Basic Auth 保护**，nginx 层 /etc/nginx/.htpasswd.monitor）

## 1. 开发目录结构（目标形态，按此组织代码）

```
monitor-panel/
├── app/
│   ├── __init__.py
│   ├── main.py            # FastAPI 入口：路由挂载、lifespan 启停调度器、/api/healthz
│   ├── config.py          # 全部配置集中：端口/周期/阈值/数据源地址/token/探测对
│   ├── db.py              # SQLite(WAL) 连接（busy_timeout/synchronous=NORMAL）、建表、采样写入、历史查询、清理
│   ├── scheduler.py       # 轻量 asyncio 调度器：间隔任务、overrun 跳过、jitter 防抖、单调时钟
│   ├── collectors/
│   │   ├── __init__.py    # 采集器注册表（周期 → collector 实例）；mock 数据开关
│   │   ├── base.py        # Collector 基类：run_once() → CollectorResult(node_id, group, metrics)
│   │   ├── frp.py         # frps dashboard API（127.0.0.1:7500 basic auth）：per-type 端点 + 降级
│   │   ├── tailscale.py   # tailscale status --json 快照 + tailscale ping 轮巡（Semaphore+超时 kill）
│   │   ├── headscale.py   # 127.0.0.1:9090 metrics 关键指标 + 8080 TCP 探活
│   │   ├── system.py      # psutil：CPU/内存/磁盘/负载/网络吞吐
│   │   ├── network.py     # 探测对执行：ICMP/TCP/HTTP/DNS（asyncio 并发）
│   │   ├── services.py    # 服务台账：约定区扫描 + 协议识别 + 探活（issue #2）
│   │   └── certs.py       # letsencrypt 证书到期扫描
│   ├── services/
│   │   ├── aggregator.py  # 最新快照缓存 + 派生指标 + 阈值判定
│   │   └── history.py     # 趋势查询：range + bucket 降采样
│   └── api/               # 只读 API（除预留 /api/ingest）
│       ├── overview.py    # GET /api/overview
│       ├── devices.py     # GET /api/devices, POST /api/devices/{id}/ping
│       ├── frp.py         # GET /api/frp
│       ├── history.py     # GET /api/history
│       ├── system.py      # GET /api/system, /api/certs, /api/network
│       ├── services.py    # GET /api/services（服务台账）
│       └── ingest.py      # POST /api/ingest（远端 agent 上报预留，本期默认 404）
├── web/
│   ├── index.html         # 单页面板（顶部状态条 + 卡片 + 表格 + 趋势图区）
│   ├── src/               # TypeScript 源码（esbuild 打包 → dist/app.js）
│   │   ├── main.ts        # 入口：hash 路由 + 轮询调度 + 视图装配
│   │   ├── table.ts       # 通用 DataTable：分页/筛选/聚合（docs/06）
│   │   ├── ui.ts          # 组件库：win/tb/st/statusLevel/esc
│   │   ├── state.ts       # 轮询结果缓存（视图读取，切换视图立即刷新）
│   │   ├── api.ts / fmt.ts / ascii.ts / types.ts
│   │   └── views/         # 视图：overview/devices/tunnels/services/alerts/…
│   ├── dist/app.js        # 构建产物（minify）
│   └── style.css
├── scripts/
│   ├── deploy.sh          # rsync → 备份 → 符号链接切换 → 健康检查 → 回滚
│   └── setup_nginx_cert.sh# nginx block + 证书双通道（--issuance http|dns）
├── tests/                 # mock 数据源下的采集器/API 冒烟测试（pytest 42 项）
│   └── table.test.ts      # DataTable 纯逻辑单测（node --test 6 项）
├── .github/workflows/ci.yml  # CI：pytest + tsc + npm test + build
├── requirements.txt       # fastapi uvicorn httpx psutil
├── monitor-panel.service  # systemd 模板（127.0.0.1:8898）
├── README.md
└── docs/                  # 本目录（方案/调研/评审/手册/约定 六份报告）
```

## 2. 开发顺序（里程碑）

| 阶段 | 内容 | 验收标准 |
|---|---|---|
| **M1 骨架** | 项目脚手架、config/db/scheduler、/api/healthz、mock 采集器、单页框架 | 本地 `uvicorn app.main:app --port 8898` 可跑；`curl localhost:8898/api/healthz` → `{ok:true}`；页面显示空面板框架 |
| **M2 数据源接入** | frp + tailscale + system 三必含采集器真实接入（字段对齐调研报告 B 节） | 本地能拉到真实数据（ssh 隧道预览或 cloud 直连）；mock 开关可切换 |
| **M3 面板完善** | overview 聚合、设备/隧道表、趋势图、证书/headscale/网络探测（F4–F8） | 完整面板：总览卡片 + 设备表 + 隧道表 + 趋势图渲染 |
| **M4 部署上线** | git → cloud 原子部署 → systemd → nginx → certbot → 健康检查 | monitor.dc-sy.cn 公网可访问；上线 checklist 全过（见 §5） |
| **M5 加固扩展** | Basic Auth、告警 webhook、cloud-2 节点接入 | 安全加固完成；第二台云接入流程可执行 |

**当前起点：M1。**

## 3. 核心设计要点（开发时必读）

### 3.1 数据模型（6 张表，均带 node_id 支持多云）

```sql
monitor_nodes(id, name, role, public_ip, enabled, last_seen_ts)   -- 'cloud-1','cloud-2'
devices(node_id, device_id, name, tailscale_ip, os, online, relay,
        last_seen_ts, rx_bytes, tx_bytes, updated_ts)              -- tailscale 设备快照
tunnels(node_id, tunnel_id, type, remote_port, local_addr, status,
        traffic_in, traffic_out, updated_ts)                      -- frp 隧道快照
samples(id, node_id, metric_group, metric_name, value, ts)        -- 时序（30天保留）
alerts(id, ts, level, source, message, resolved)                  -- 告警流（90天）
services(id, node_id, device_id, device_name, tailscale_ip, port, -- 服务台账（issue #2）
         name, auto_name, protocol, url, http_status, latency_ms,
         status, missing_rounds, source, enabled,
         first_seen_ts, last_seen_ts, updated_ts)                 -- 端口约定驱动自动发现
```

> 服务台账机制：自定义项目/网站统一绑定约定区（默认 10000–19999），面板自动扫描
> 发现 + 探活；区间外固定端口走 manual 登记。详见 `docs/05-服务台账与端口约定.md`。

### 3.2 API（统一 `{ok, data, ts}`；公网经 nginx Basic Auth 保护）

| 端点 | 说明 |
|---|---|
| GET /api/overview | 聚合总览（前端主轮询点） |
| GET /api/devices | tailscale 设备列表 |
| POST /api/devices/{id}/ping | 手动 ping（只读，限流 5s/设备） |
| GET /api/frp | frps 隧道列表+流量 |
| GET /api/system /api/certs /api/network | 资源/证书/探测 |
| GET /api/services | 服务台账（自动发现 + 探活状态 + 直达链接 + 下线告警） |
| GET /api/history?metric=&range=&bucket= | 趋势降采样 |
| GET /api/healthz | 探活（本机直连 127.0.0.1，nginx 认证不涉及） |
| POST /api/ingest | 预留（本期 404） |

> 公网访问 `https://monitor.dc-sy.cn` 需 Basic Auth（凭据文件 /etc/nginx/.htpasswd.monitor；
> ACME 挑战路径已豁免认证）。本机 127.0.0.1:8898 直连无认证（部署健康检查走此通道）。

### 3.3 采集要点（评审确认的硬要求）

- **frp**：frps dashboard 用 **per-type 端点**（`/api/proxy/tcp` 等，非 `/api/proxy/all`）；失败降级到 8899 /api/status
- **tailscale ping**：每节点 3 包取中位/最小；**超时语义**：在线+超时=warn 3 轮防抖，离线不告警（防休眠设备误报）；`asyncio.Semaphore(2)` 限并发；子进程超时 kill
- **脱敏**：`sanitize()` 过滤 node_key/disco_key/公钥指纹（env 注入凭据，文件 600）
- **权限**：monitor 用户 + sudoers NOPASSWD 白名单（tailscale status/ping、headscale nodes list 3 条只读命令，含参数校验）
- **SQLite**：busy_timeout、WAL、synchronous=NORMAL；写入批量事务；每小时清理+checkpoint
- **调度器**：overrun 跳过（上次未完成则跳过本轮）、jitter 防抖、`time.monotonic()`

### 3.4 阈值默认（config.py 集中）

ts 延迟 >200ms warn / >500ms crit；丢包 >2%/5%；证书 <30d/<7d；磁盘 >85%/92%；frp 隧道离线即 warn。

## 4. 环境依赖与本地开发

```bash
# 本地（WSL）开发环境
cd /home/dc/projects/monitor-panel
uv venv .venv && source .venv/bin/activate
uv pip install -r requirements.txt          # fastapi uvicorn httpx psutil
uvicorn app.main:app --port 8898            # 本地预览（collectors mock=True）
```

- 本地开发默认 `mock=True`（无需 cloud 依赖即可跑通全部 API/UI）
- 真实数据联调：`ssh -L 8898:127.0.0.1:8898 dc-sy` 隧道（cloud 部署后）或本地直连 cloud 数据源

### 4.1 配置管理（issue #1 统一）

**单一权威源**：生产配置只维护 `/opt/monitor-panel/env`（chmod 600，
systemd `EnvironmentFile` 与 `app/config.py` 双通道读同一文件）。
release 目录内不再有 `.env` 副本，release 切换不丢配置。

env 文件加载顺序（`app/config.py`，`setdefault` 语义——已存在的环境变量始终优先）：

1. `MONITOR_ENV_FILE` 显式指定路径
2. `/opt/monitor-panel/env`（生产权威源）
3. 仓库根 `.env`（本地开发回退）

**启动校验（fail-fast）**：live 模式下 `MONITOR_FRPS_PASS` / `MONITOR_LEGACY_TOKEN`
缺失直接拒启（防止配置丢失静默上线空数据面板）；`scripts/deploy.sh` 健康检查
同时校验 `ok:true` 与 `mode ∈ {live, snapshot}`，回落 mock 视为部署失败自动回滚。

完整变量清单见 `.env.example`（与 `app/config.py` 一一对应）。

## 5. 上线 Checklist（M4，我审查时逐项核对）

- [ ] DNS：monitor.dc-sy.cn → 124.221.190.237（用户添加）
- [ ] 安全组：80/443 放行；8898/7500/9090 **无公网入站**（腾讯云控制台确认）
- [ ] nginx：monitor.dc-sy.cn server block → 127.0.0.1:8898；**含 N1 修正**：
      `location ^~ /.well-known/acme-challenge/ { auth_basic off; }`（Basic Auth 豁免 ACME）
- [ ] 证书：预检 8a（外部 curl -sI 探测 webblock）→ HTTP-01 优先 / DNS-01 兜底（acme.sh dns_ali，阿里云 RAM AK 仅 DNS 权限，凭据 600 不入 git）
- [ ] systemd：monitor-panel.service（monitor 用户，127.0.0.1:8898，Restart=on-failure）
- [ ] 日志轮转 + 每日 db 备份 timer（backup-db.timer 身份/权限核对）
- [ ] nginx/letsencrypt 配置快照（部署前 tar）
- [ ] /api/healthz 200（本机 127.0.0.1 验证，避免 Basic Auth 干扰）
- [ ] 页面截图核验（总览卡片/设备表/隧道表/趋势图/状态灯）
- [ ] 部署原子切换 + 回滚预案（releases/<ts> + current 软链）

## 6. 参考文档（docs/）

| 文件 | 内容 |
|---|---|
| `01-技术方案-v1.1.md` | 完整技术方案（架构/功能/API/数据模型/部署/扩展/风险，514 行） |
| `02-数据源调研报告.md` | 数据源实测（frp 面板资产、tailscale/headscale 字段、采集频率、多云结论） |
| `03-评审与复审报告.md` | t3 评审（阻塞项+修正项）与 t5 复审（通过+N1 修正） |
| `04-设备接入手册.md` | 设备接入（阿里云/树莓派/子网路由/扩展预留） |
| `05-服务台账与端口约定.md` | 服务台账机制（端口分区约定/自动发现/域名探测） |
| `06-前端表格组件规范.md` | DataTable 通用组件（接入教程/规则/踩坑记录） |

## 7. 开发完成后交接

开发完成后（或每个里程碑），通知我审查：
1. 代码审查（结构/安全/脱敏/权限）
2. 对照上线 Checklist 逐项核验
3. 通过后执行 M4 部署（我走 linux-ops 原子部署流程）

## 8. 审查修复记录（2026-08-15，v0.1.1）

| # | 问题 | 修复 | 验证 |
|---|---|---|---|
| 1 | 🔴 tailscale ping 正则未转义（`(S+)`/`[d.]`）→ 真实模式延迟永远解析失败 | `_PONG_RE`/`_LAST_SEEN_RE` 改为 `\S+`/`[\d.]+` | 真实输出 3 例 + 垃圾输入 3 例测试通过 |
| 2 | 🟠 device_id 原始节点 ID（含敏感指纹）入库/API 未脱敏 | `_norm_device` 用 `_hash_id()` sha256 截断 8 位 | 稳定性/唯一性/不残留测试通过 |
| 3 | 🟡 `_on_frp_link` 数据缺失（None）时误 resolve 告警 | 仅两项数据齐全且未超阈值才 resolve | 缺失不误判/超阈值告警/恢复 resolve 测试通过 |

新增测试：`test_collectors.py`（4 个回归用例）+ `test_aggregator_fix.py`（2 个）。共 33 测试全过。
