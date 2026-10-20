---
mdlineage:
  schema: 1
  id: monitor-panel.cloud2-ingest-runbook
  kind: guide
  status: active
  topics: [ingest, multi-node, runbook]
  created_at: 2026-08-22
  updated_at: 2026-08-22
  relations:
    - type: depends_on
      target: monitor-panel.tech-design
      reason: ingest 上报契约源自技术方案的多节点二期设计
---

# 08 — cloud-2 接入手册（ingest 上报，issue #5 后续）

> 建立：2026-08-22（issue 清零批次）。前置：issue #5 已落地——nginx 已豁免
> `/api/ingest` 的 Basic Auth，应用层 Bearer token 防护就绪，`INGEST_ENABLED` 默认 false。
> 第二台云服务器（cloud-2）接入远端 agent 上报时按本手册执行，**无需再动 nginx**。

## 1. 通道现状（已就绪，勿重复施工）

```
cloud-2 agent ──HTTPS POST──▶ https://monitor.dc-sy.cn/api/ingest
                               │ nginx location = /api/ingest：
                               │   auth_basic off（豁免 Basic Auth）
                               │   limit_except POST { deny all; }
                               ▼
                          FastAPI 应用层：Bearer token（hmac.compare_digest）
                               ▼
                          Aggregator.ingest() → samples/snapshots/告警
```

- nginx 配置：`/etc/nginx/sites-enabled/monitor`（仓库权威源 `scripts/setup_nginx_cert.sh`，
  含豁免块；历史备份在服务器 `~/nginx-backups/`）
- 应用层：`app/api/ingest.py`；未启用时返回 404（接口不可发现），启用后无/错 token 返回 401

## 2. 启用步骤（cloud 侧，一次性）

1. 生成随机 token：`openssl rand -hex 32`
2. 编辑 `/opt/monitor-panel/env`（生产权威源）追加：

   ```ini
   MONITOR_INGEST_ENABLED=1
   MONITOR_INGEST_TOKEN=<上一步生成的 hex>
   ```

3. 重启服务：`sudo systemctl restart monitor-panel`
4. 验证：`curl -s -X POST -H "Authorization: Bearer <token>" \
   -H "Content-Type: application/json" -d '{"results":[]}' \
   https://monitor.dc-sy.cn/api/ingest` → 期望 `{"ok":true,"data":{"accepted":0},...}`

## 3. agent 上报契约（与自采 CollectorResult 同构）

```json
POST /api/ingest
Authorization: Bearer <token>
Content-Type: application/json

{
  "results": [
    {
      "node_id": "cloud-2",
      "group": "system",
      "metrics": [
        {"name": "cpu_percent", "value": 12.5, "ts": 1700000000.0},
        {"name": "disk_percent./data", "value": 61.0}
      ],
      "payload": {"cpu_percent": 12.5, "disk": {"/data": {"percent": 61.0}}}
    }
  ]
}
```

- `group` 必须在指标白名单内（`app/constants.py GROUP_METRICS`），
  指标名须为白名单基础名或 `<base>.<suffix>` 形态，否则 samples 整批拒绝
- `payload` 可选；写入聚合层内存快照（overview 展示用）
- 响应：`{"ok": true, "data": {"accepted": <n>}, "ts": ...}`；
  401=token 错 / 400=结构错 / 404=未启用

## 4. cloud-2 侧建议采集项

| group | 指标 | 说明 |
|---|---|---|
| `system` | cpu_percent / mem_percent / disk_percent.<mount> / load_* | psutil 本机 |
| `network` | latency_ms.<probe> / reachable | 与 cloud-1 对等探测（probes 配置加对端） |

周期建议 30–60s；agent 端失败静默重试即可（服务端无去重窗口，重复上报仅多写样本）。

## 5. 关闭/轮换

- 临时关闭：env 改 `MONITOR_INGEST_ENABLED=0` + 重启（接口回到 404 不可发现）
- token 轮换：env 换新值 + 重启 + 分发新值给 agent（旧 token 立即失效）
- 彻底收回公网通道：删 nginx `location = /api/ingest` 块 + reload（同时更新 setup_nginx_cert.sh 保持同源）

## 6. 测试

`tests/test_ingest.py`：404（未启用）/ 401（无或错 token）/ 200（正确 token 接受）/ 400（坏 body）。

## 7. aliyun frps 接入（2026-10-20）

针对第二台 frps（node_id="aliyun"，本地 dashboard 运行于 127.0.0.1:7400），部署 stdlib-only 上报 agent（`scripts/ingest_agent.py`）。

### 7.1 面板端配置（无任何 nginx 改动）
1. 确认主节点注册包含 `aliyun`：编辑 `/opt/monitor-panel/env`，设置或更新 `MONITOR_NODES`：
   ```json
   MONITOR_NODES=[{"id":"cloud-1","name":"cloud-1","role":"primary","public_ip":"124.221.190.237","enabled":true},{"id":"aliyun","name":"aliyun","role":"secondary","enabled":true}]
   ```
2. 确认开启 ingest 并配置 token（若此前未开启）：
   ```ini
   MONITOR_INGEST_ENABLED=1
   MONITOR_INGEST_TOKEN=<hex token>
   ```
3. 重启面板：`sudo systemctl restart monitor-panel`

### 7.2 aliyun 节点部署
1. 安装目录：`/opt/frp-ingest-agent/`
2. 分发脚本：将仓库的 `scripts/ingest_agent.py` 同步到 `/opt/frp-ingest-agent/ingest_agent.py` 并赋予执行权限。
3. 配置环境变量文件 `/opt/frp-ingest-agent/env`（`chmod 600`）：
   ```ini
   INGEST_URL=https://monitor.dc-sy.cn
   INGEST_TOKEN=<上述生成的 hex token>
   NODE_ID=aliyun
   FRPS_URL=http://127.0.0.1:7400
   FRPS_USER=admin
   FRPS_PASS=<aliyun frps dashboard 密码>
   HTTP_TIMEOUT=10.0
   ```
4. 定时任务（cron 行，推荐每分钟执行单轮）：
   ```cron
   * * * * * set -a; . /opt/frp-ingest-agent/env; set +a; /usr/bin/python3 /opt/frp-ingest-agent/ingest_agent.py --once >> /var/log/frp-ingest-agent.log 2>&1
   ```
   *注：亦可通过 systemd 服务常驻循环运行（去掉 `--once`，默认按 `INGEST_INTERVAL=60` 周期运行）。*

