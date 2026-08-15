# monitor.dc-sy.cn 统一监控面板 — 开发计划与任务书

> 版本：v1.0 ｜ 日期：2026-08-15 ｜ 状态：**M1–M3 代码完成（2026-08-15），待 M4 部署上线**
> 本文档是开发工作区的主指引；详细设计见 docs/ 下三份团队报告。

## 开发状态（2026-08-15 更新）

| 里程碑 | 状态 | 说明 |
|---|---|---|
| M1 骨架 | ✅ 完成 | 脚手架、config（env 注入）/constants（指标白名单）/db（WAL+PRAGMA）/scheduler（overrun+jitter+单调时钟）、/api/healthz、mock 采集器、单页框架 |
| M2 数据源接入 | ✅ 完成 | frp（per-type 端点 + /api/proxy/all 探测 + 降级 8899）、tailscale（status+ping 轮巡，Semaphore(2)+超时 kill+3 包中位）、system（psutil+to_thread）、脱敏层 sanitize()；sudoers 白名单已备（deploy/） |
| M3 面板完善 | ✅ 完成 | overview 聚合、设备表（device_map 对照）、隧道表、链路质量表、趋势图（降采样）、证书/headscale/网络探测、告警流、手动 ping（限流 5s/设备） |
| M4 部署上线 | ⏳ 待执行 | 见 §5 Checklist + docs/01 §6（安全组确认 → 原子部署 → systemd → nginx → 预检 8a → 双通道签发）；脚本已备：scripts/deploy.sh、scripts/setup_nginx_cert.sh |
| M5 加固扩展 | ⏳ 二期 | Basic Auth 复核、告警 webhook、8899 加固（需用户审批）、cloud-2 接入（ingest 已预留，默认 404） |

**本地开发**：

```bash
cd /home/dc/projects/monitor-panel
uv venv .venv && source .venv/bin/activate
uv pip install -r requirements.txt          # fastapi uvicorn httpx psutil
uvicorn app.main:app --port 8898            # 本地预览（collectors mock=True）
# 冒烟测试
uv pip install -r requirements-dev.txt && python -m pytest tests/ -q
```

**M4 部署入口**（用户侧前置：DNS A 记录、备案确认、阿里云 RAM AK）：

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

**技术栈**：Python FastAPI + uvicorn（单进程）+ SQLite(WAL) + 原生 JS + Chart.js（vendored）
**端口**：8898（仅绑 127.0.0.1，nginx 反代 443）
**访问**：https://monitor.dc-sy.cn（DNS 待添加 → 124.221.190.237）

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
│       └── ingest.py      # POST /api/ingest（远端 agent 上报预留，本期默认 404）
├── web/
│   ├── index.html         # 单页面板（顶部状态条 + 卡片 + 表格 + 趋势图区）
│   ├── app.js             # 轮询/渲染/图表/阈值配色
│   ├── chart.umd.min.js   # vendored Chart.js（本地，避免 CDN 依赖）
│   └── style.css
├── scripts/
│   ├── deploy.sh          # rsync → 备份 → 符号链接切换 → 健康检查 → 回滚
│   └── setup_nginx_cert.sh# nginx block + 证书双通道（--issuance http|dns）
├── tests/                 # mock 数据源下的采集器/API 冒烟测试
├── requirements.txt       # fastapi uvicorn httpx psutil
├── monitor-panel.service  # systemd 模板（127.0.0.1:8898）
├── README.md
└── docs/                  # 本目录（方案/调研/评审三份报告）
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

### 3.1 数据模型（5 张表，均带 node_id 支持多云）

```sql
monitor_nodes(id, name, role, public_ip, enabled, last_seen_ts)   -- 'cloud-1','cloud-2'
devices(node_id, device_id, name, tailscale_ip, os, online, relay,
        last_seen_ts, rx_bytes, tx_bytes, updated_ts)              -- tailscale 设备快照
tunnels(node_id, tunnel_id, type, remote_port, local_addr, status,
        traffic_in, traffic_out, updated_ts)                      -- frp 隧道快照
samples(id, node_id, metric_group, metric_name, value, ts)        -- 时序（30天保留）
alerts(id, ts, level, source, message, resolved)                  -- 告警流（90天）
```

### 3.2 API（只读，统一 `{ok, data, ts}`）

| 端点 | 说明 |
|---|---|
| GET /api/overview | 聚合总览（前端主轮询点） |
| GET /api/devices | tailscale 设备列表 |
| POST /api/devices/{id}/ping | 手动 ping（只读） |
| GET /api/frp | frps 隧道列表+流量 |
| GET /api/system /api/certs /api/network | 资源/证书/探测 |
| GET /api/history?metric=&range=&bucket= | 趋势降采样 |
| GET /api/healthz | 探活 |
| POST /api/ingest | 预留（本期 404） |

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
