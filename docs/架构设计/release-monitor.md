---
mdlineage:
  schema: 1
  id: monitor-panel.release-monitor
  kind: spec
  status: active
  topics: [releases, grm, integration]
  created_at: 2026-08-22
  updated_at: 2026-08-22
  relations:
    - type: implements
      target: monitor-panel.tech-design
      reason: 落实技术方案"留口子"多源扩展设计，作为首个非监控数据源
---

# 07 — Release 监控区块（issue #4：grm 只读聚合）

> 建立：2026-08-22（issue 清零批次）。适用范围：`app/collectors/grm.py` / `app/db.py grm_repos` /
> `app/api/grm.py` / `web/src/views/releases.ts`。线上版本：release 20260822-195935。

## 1. 定位与边界（最重要）

monitor-panel 与 github-release-monitor（grm，https://releases.monitor.dc-sy.cn ，
Next.js + SQLite，127.0.0.1:8901）同机部署于 cloud，同属"监控门户"但监控内容不同。

| 职责 | 归属 |
|---|---|
| 仓库增删 / 通知渠道 / README·Commit 等扩展配置 | **grm 侧**（唯一配置入口） |
| 聚合展示仓库状态、最新 release、降级提示 | **本面板**（只读，无任何写操作） |

本区块**不提供任何配置入口**；grm 侧改动后最多一个采集周期（默认 5min）自动反映到本面板。

## 2. 数据链路

```
grm 写 /opt/github-release-monitor/data/repositories.json（Repository[]）
        │  同机文件读取（实测 644 全局可读 → monitor 用户零权限改动）
        ▼
GrmCollector._run_real()（5min，MONITOR_INTERVAL_GRM）
        │  _normalize()：grm 字段 → grm_repos 行
        ▼
Aggregator._on_grm() → db.upsert_grm_repos()（快照表 + prune）
        ▼
GET /api/grm（db 为权威源）→ 前端 releases 视图 + overview 摘要行
```

指标白名单（`constants.GROUP_GRM`）：`repos_total` / `repos_with_release` / `repos_new`。

## 3. 字段映射（grm `Repository` → `grm_repos`）

| grm 字段 | 本面板字段 | 说明 |
|---|---|---|
| `id`（`<provider>[:instance]:owner/repo`） | `repo_id` / `owner_slug` | owner_slug 取最后一个冒号后最后两段，兼容自建 GitLab |
| `displayName` | `display_name` | 卡片标题优先用它 |
| `tags[]` | `tags`（JSON 数组存 TEXT） | 卡片徽标 |
| `isPinned` / `isNew` | `is_pinned` / `is_new` | 置顶排序 / NEW 徽标 |
| `latestRelease.tag_name/.name/.html_url/.prerelease/.published_at` | `latest_*` / `published_ts` | ISO8601 → epoch；缺失 = 无 release |
| `readmeSummary` / `recentCommits`（fork A/B/C 扩展，**尚未就绪**） | `readme_summary` / `recent_commits` | 就绪前恒空 → 前端降级占位 |

## 4. 降级语义（照 frp 先例）

- **文件缺失 / JSON 损坏 / 结构异常**：payload 带 `degraded:true + error`，正常返回——
  仅 Release 区块显示"暂不可用"，其余监控不受影响
- **降级轮次不清快照**：`_on_grm` 遇 degraded 直接 return，`grm_repos` 保留上一轮数据
  （不能用空列表 prune，否则短暂故障会清掉全部卡片）
- **grm 侧删除仓库**：非 degraded 轮次 prune（本轮未见到的行删除）——grm 是清单唯一权威源

## 5. 配置

| 变量 | 默认 | 说明 |
|---|---|---|
| `MONITOR_GRM_DATA_PATH` | `/opt/github-release-monitor/data/repositories.json` | 数据文件路径 |
| `MONITOR_GRM_BASE_URL` | `https://releases.monitor.dc-sy.cn` | 前端"配置入口/详情"跳转地址 |
| `MONITOR_INTERVAL_GRM` | `300` | 采集周期（秒） |

## 6. fork 扩展联动（A/B/C 系列）

grm 侧 `readmeSummary` / `recentCommits` 字段落地后：
1. `_normalize()` 已透传（`repo.get("readmeSummary")` / `repo.get("recentCommits")`）——**无需改本面板代码**
2. 前端卡片自动从"无 release · 待扩展"占位切换为真实摘要
3. 若字段命名与预期不符，仅需调整 `_normalize()` 两行映射

## 7. 排障

| 症状 | 排查 |
|---|---|
| 区块显示"Release 监控暂不可用" | `curl 127.0.0.1:8898/api/grm` 看 `summary.error`；服务器 `ls -la $MONITOR_GRM_DATA_PATH`（权限/存在性）；grm 服务是否在写文件 |
| 卡片数量与 grm 面板不一致 | 等一个采集周期（5min）；仍不一致查 `/api/healthz` collectors.grm errors/last_error |
| 无 release 仓库只显示占位 | 预期行为：fork 扩展字段（readme/commits）尚未就绪 |
| 本地 snapshot 模式无数据 | `snapshots/grm.json` 需存在（仓库已带 mock 快照；真实快照经 `scripts/fetch_snapshot.sh` 抓取） |

## 8. 测试

`tests/test_grm.py`（六段式）：mock 结果 / owner_slug 提取 / 真实文件读取 /
双降级路径（缺失+损坏）/ db upsert+prune / degraded 保快照 / API 契约 / overview 区块。
