---
mdlineage:
  schema: 1
  id: monitor-panel.terraform-reuse-research
  kind: report
  status: active
  topics: [terraform, architecture, certificates, integration]
  created_at: 2026-10-20
  updated_at: 2026-10-20
  relations:
    - type: related_to
      target: monitor-panel.tech-design
      reason: 评估技术方案采集层各功能改用 terraform 实现的边界，回应聚合展示定位
    - type: related_to
      target: monitor-panel.arch-requirements-review-20261020
      reason: 承接其 R2 结论——面板聚焦聚合展示，采集供给方式需重新划分
---

# terraform 功能替代调研报告（2026-10-20）

> 调研方式：glm-5.3 与 Atria-Dawn 两个独立模型按同一任务书调研（Atria 按"并发调用工具"要求执行），
> 主会话对关键事实逐条核实后才写入本报告。目标（用户原话）：监控各服务器、监控域名及更新证书等
> 功能尽量复用 terraform，monitor-panel 聚焦"聚合多源数据并展示"。

## 1. 核心结论（双模型一致）

monitor-panel 的功能中只有**证书签发/续期**真正具备"声明式基础设施"语义；**跨机系统巡检**
可由现有 terraform audit 管线承担采集（B 类）；其余采集器与告警聚合因**时效性（30s/60s）、
localhost 数据源约束、有状态防抖逻辑**三项硬约束必须保留常驻进程。"聚合展示"目标经
`/api/ingest`（生产已启用，2026-10-20 实测）零面板改动达成。

**用户直觉的主要偏差点**：terraform 是"拉起式"工具（每次 apply 才运行，本机 WSL 触发），
没有调度器、没有常驻心跳——它天然适合管理"配置与状态漂移"，不适合承担"节拍与实时性"。
监控与续期、配置与采集、展示与聚合三层职责拆开后，terraform 的正确位置就清楚了（见 §5）。

## 2. 事实基线（主会话逐条核实，含对任务书假设的修正）

| 事实 | 核实结果 | 影响 |
|---|---|---|
| dc-sy.cn 的 NS 归属 | **实测 `dig NS` = dns31/32.hichina.com（阿里云）**，与技术方案 §D 实测记录一致；任务书"域名在腾讯云"为错误假设 | DNS-01 必须走阿里云 API（现网脚本 `acme.sh --dns dns_ali` 正是如此）；**tencentcloud provider 管不了本域 DNS**，glm 的"DNS 记录纳管 DNSPod"建议修正为 aliyun provider 或子域 NS 委托 |
| headscale 运行位置 | **cloud 本机**（`HEADSCALE_METRICS_URL=127.0.0.1:9090`，audit 显示 cloud headscale active；pi 仅有 tailscaled） | terraform `variables.tf` 中 pi 的"(Headscale)"描述已过时，建议顺手更正；headscale 采集走 cloud 回环，WSL 探针无法替代 |
| terraform state 私钥 | `terraform.tfstate` 含 `private_key_openssh` 明文（tls_private_key 资源语义必然）；**linux-ops 非 git 仓库**，无提交泄漏面 | 现存风险=本地磁盘明文；若该目录未来入 git/网盘同步须先处理（历史清除+密钥轮换）；acme 迁移会把 LE 账户/证书/DNS 凭据再聚合进 state，属安全劣化 |
| 生产 ingest | `INGEST_ENABLED=1`、token 非空（空 POST 实测 401） | terraform 产物经 ingest 入库的通道**现成可用**，面板侧零改动 |
| WSL 运行环境 | terraform 在 WSL 本机，随 Windows 睡眠/关机停止 | WSL 承担定时采集则数据覆盖率受开机时长限制（按每日 8h 睡眠估算约 67%），节假日连续多日断采 |
| audit 管线现状 | `terraform apply -auto-approve` 约 4s，5 机并发 SSH，无定时器 | 具备承担 B 类采集的全部基础，缺的只是节拍（cron）与对接（ingest） |

## 3. 功能判定矩阵

| 功能 | 周期 | glm 分类 | Atria 分类 | 一致性 | 采信结论 |
|---|---|---|---|---|---|
| system（本机 psutil 30s 趋势） | 30s | C | C（cloud 本机部分） | 一致 | **C 保留**：带宽速率需 30s 差值状态，本机直采零成本 |
| 跨机系统巡检（面板没有的能力） | 手动 apply | B | B | 一致 | **B terraform 采集**：audit_all.py 已覆盖 4+1 台硬件/负载/磁盘/服务状态，是"面板聚合 terraform 产物"的最佳切入点 |
| frp（frps dashboard + 8899 legacy） | 30s | C | C | 一致 | **C 保留**：数据源绑定 cloud 回环（7500/8899），WSL 探针需叠 SSH 隧道，毫无收益；frp 0.70 端点坑已在面板侧填平 |
| tailscale 设备+ping 轮巡 | 30s/60s | C | C | 一致 | **C 保留**：在线才 ping、3 轮防抖、手动 ping 限流全是进程内状态（评审 E3 刻意设计） |
| headscale 控制面 | 30s | C | C | 一致 | **C 保留**：localhost 数据源 + 全网单点控制面需持续可见性 |
| certs 证书到期监控 | 3600s | B（可选） | B（可选，收益≈0） | 一致 | **建议保留面板采集**：TLS SNI 外部视角 + nginx 动态发现已完整，1h 频率与 terraform 拉起节奏匹配但无功能增量；terraform `tls_certificate` 可作第二视角补充（可选档） |
| network 公网探测对 | 60s | C | B（语义改变） | **分歧** | 采信 Atria + 限定条件：迁 WSL 后语义从"cloud 出站视角"变"家宽视角"，是有价值的新维度但**不可替代原语义**；若做，作为独立 node_id 经 ingest 并入，原采集器保留 |
| services 服务台账探活 | 60s | C（台账单源化 A） | C | 一致 | **C 保留**：分片游标 + DB 生命周期 + 误报治理（1199 条教训）是演进资产，重写即倒退；"台账声明配置化"留作远期想法 |
| grm release 聚合 | 300s | C | C（聚合展示样板） | 一致 | **C 保留**：它本身就是"别的服务采集、面板只读展示"的目标形态样板，terraform 插入只加层级 |
| scheduler + Aggregator 告警 | 常驻 | C | C | 一致 | **C 保留（核心）**：冷却/防抖/resolve 是面板核心价值，且 ingest 数据入库自动获得告警能力 |
| API + 前端 | 5-10s 轮询 | C | C | 一致 | **C 保留**：用户目标的保留本体 |
| 证书续期（certbot/acme.sh，服务器侧） | timer/cron | A（有保留条件）/不建议整体迁 | A（技术上）/建议不迁 | 一致（均不建议） | **续期留服务器，terraform 最多管"新域名首次签发"**（详见 §4） |

## 4. 域名与证书专项（双模型结论一致，事实已修正）

**原则：到期监控（只读探针）与续期（写操作）必须分开评估，结论相反。**

- **到期监控：保留面板采集器。** 现有实现（TLS SNI 外部视角 + nginx -T 动态发现 + openssl 本地
  回退 + 30d/7d 阈值告警闭环）已完整且是黑盒外部视角，与续期方式解耦。terraform `tls_certificate`
  data source 可从家宽视角做第二视觉复核（可选档），不替换。
- **续期：不建议迁入 WSL terraform**，四重理由：
  1. **无调度器**：terraform 只在手动 apply 时运行；证书 90 天有效、`min_days_remaining=30` 意味着
     60 天窗口内必须有人开着 WSL 跑一次 apply——严格劣于服务器上常驻的 certbot timer / acme.sh cron。
  2. **NS 事实修正**：dc-sy.cn NS 实测在阿里云 hichina，DNS-01 凭据是**阿里云 RAM AK**；
     现网 `acme.sh --dns dns_ali` 已是正确通道。glm 提议的 DNSPod/腾讯云通道对本域不可用。
  3. **state 明文聚集**：迁移会把 LE 账户私钥、8+ 张证书私钥、DNS API 凭据全部聚合进
     terraform.tfstate——把服务器上分散的 600 权限凭据变成一个高价值蜜罐文件，安全劣化
     （现 state 已含 deploy_key 明文，见 §2）。
  4. **LE 速率风险**：terraform state 任何漂移可能触发整批重签（每注册域名每周 50 张上限，
     8 站点安全余量有限）。
- **terraform 在证书领域的正确位置**：新域名首次签发（issuance of record，低频有人盯）可选走
  acme provider；DNS **记录**纳管（A/CNAME/TXT）可用 aliyun provider（需新增 provider）或
  子域 NS 委托后用 tencentcloud——属可选档，先解决凭据入 state 问题再谈。

## 5. 运行模式与可靠性（双模型一致，Atria 方案更完整）

- **高频 cron 跑 `terraform apply` 是反模式**（双模型一致否决）：timestamp trigger 使 state 每次
  dirty/膨胀、4s apply×高密度=纯开销、并发锁竞争。
- **正确的职责三分**（采信 Atria）：**terraform 管"脚本与 crontab 的声明式分发"（防配置漂移）、
  cron 管"节拍"（放 cloud 服务器，7×24 在线）、面板管"聚合展示"**。audit_all.py 的探针代码与
  vantage 无关，把 SERVERS 列表改为 cloud→各机现有 SSH 通道即可搬到 cloud cron，WSL 关机完全不影响。
- **cron 频率建议**：5 分钟（跨机慢速台账语义：硬件/磁盘/uptime 以小时计，load/mem 5min 足够发现
  "机器跪了"）；与面板 30s/60s 高频层是**分层关系而非替代**。
- **WSL 直跑定时采集的短板**（若坚持 WSL）：数据覆盖率随开关机波动、断采本身不可见（需面板加
  ts 陈旧检测）、`latency_ms` 测的是"家宽→服务器"而非服务器健康。

## 6. 不建议迁移清单（合并后 8 条）

1. tailscale ping 轮巡 + 手动 ping（进程内状态 + 交互功能）。
2. services 服务台账（游标/生命周期/误报治理三件套）。
3. frps dashboard / headscale metrics 采集（localhost 数据源）。
4. Aggregator 告警引擎（面板核心价值，ingest 复用它）。
5. grm 聚合（目标形态样板，插入 terraform 只加层）。
6. 网络吞吐 kbps（30s 差值状态，cron 化即丢失）。
7. 证书续期全迁 WSL terraform（§4 四重理由）。
8. 高频 cron apply 模拟实时采集（反模式）。

## 7. 迁移路线（三档）

### 🟢 立即档（低成本、双模型一致）
1. **跨机巡检入面板**：cron（cloud 服务器，5min）跑 audit_all.py → 结果经 `/api/ingest` 入库
   （node_id=`tf-audit` 或每机一节点）→ 面板加"服务器台账"视图。面板侧改动仅一个只读视图 +
   ingest 侧零改动（契约同构）；audit 脚本加 ~30 行 POST 适配。
2. **terraform 更正 pi 的"(Headscale)"过时描述**（headscale 实际在 cloud）。
3. **state 卫生**：确认 linux-ops 目录不入 git/网盘（目前非 git 仓库，安全）；如未来纳管先做
   密钥轮换方案。

### 🟡 可选档（有收益、需先验证）
4. **DNS 记录纳管**：先定凭据方案（aliyun provider 管理 hichina DNS，或子域 NS 委托），并解决
   DNS API 凭据避开 state 明文（env 注入路径待验证）。
5. **证书第二视角**：`tls_certificate` data source 从家宽视角复核 8 域名，经 ingest 入库为独立
   node_id（与面板 TLS SNI 视角互补）。
6. **新域名首签走 terraform acme provider**（续期仍留服务器 timer）。
7. **network 家宽视角探测**（WSL cron → 独立 node_id 入库，不替换原采集器）。

### 🔴 不建议档
8. 证书续期全迁、§6 清单 1-6 与 8。

## 8. 对接方式（分歧与仲裁）

| 方案 | glm 评价 | Atria 评价 | 仲裁 |
|---|---|---|---|
| 面板读 `generated/audit_report.json` | 推荐起步 | ✗ 不推荐（需 WSL→cloud 推送通道 + 结构不对齐） | **采信 Atria**：传输与契约成本高，且 WSL 是单点 |
| `snapshots/*.json` | 仅适合演示 | ⚠ 陷阱（静态数据、停掉全部自采与告警） | 一致否决（生产不可用） |
| **`/api/ingest`** | 中期最优 | ✅ 首选（生产已启用、契约同构、告警全链路复用） | **一致采纳为首选**：cron 在 cloud 侧可走 `127.0.0.1:8898` 直连，WSL 侧走 https+Bearer |

## 9. 待验证疑点（遗留）

1. **现网 8+ 域名的实际续期通道**：certbot timer（http-01）还是 acme.sh cron（dns-01）在跑？
   决定"续期留服务器"的具体基线（技术方案 E1 的 webblock 闭环状态）。
2. WSL 是否常驻 systemd/cron（影响可选档 7 的可行性；结论已按"放 cloud"给出，不阻塞）。
3. acme provider（lego 系）DNS 凭据能否经 env 注入避开 state 明文（可选档 4/6 的前置）。
4. terraform 侧采集进面板的 group/node 命名规范（建议建 `infra` 命名空间，避免与 collector
   group 冲突——与架构审查报告 M6 的命名空间隔离建议合并处理）。
