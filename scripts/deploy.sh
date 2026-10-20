#!/usr/bin/env bash
# ============================================================
# monitor-panel 原子部署脚本（M4）
#   本地 → rsync 到 releases/<ts> → 备份 → 符号链接切换 →
#   systemctl restart → 健康检查（ok + mode 非 mock）→ 失败自动回滚 →
#   旧 release 清理（保留最近 5 个，current 指向除外）
# 用法:
#   ./scripts/deploy.sh                      # 部署当前代码
#   ./scripts/deploy.sh --rollback           # 回滚到上一 release
#   ./scripts/deploy.sh --host dc-sy --remote-dir /opt/monitor-panel
# 注意: 健康检查走本机 127.0.0.1（N2：绕开 nginx/SSO 门禁）
# 部署机 ubuntu 用户所需 sudoers 白名单见 deploy/sudoers.deploy（monitor-deploy-ctl）
# ============================================================
set -euo pipefail

LOCAL_DIR="$(cd "$(dirname "$0")/.." && pwd)"
HOST="dc-sy"
REMOTE_DIR="/opt/monitor-panel"
ROLLBACK=0

while [ $# -gt 0 ]; do
  case "$1" in
    --rollback) ROLLBACK=1; shift ;;
    --host) HOST="$2"; shift 2 ;;
    --remote-dir) REMOTE_DIR="$2"; shift 2 ;;
    *) echo "未知参数: $1"; exit 2 ;;
  esac
done

CURRENT="$REMOTE_DIR/current"
TS="$(date +%Y%m%d-%H%M%S)"
RELEASE="$REMOTE_DIR/releases/$TS"
BACKUPS="$REMOTE_DIR/backups"

SSH_OPTS=(-o ConnectTimeout=15 -o ServerAliveInterval=10 -o ServerAliveCountMax=3 -o TCPKeepAlive=yes)

health_check() {
  echo "→ 健康检查 (127.0.0.1:8898/api/healthz)…"
  # 双重校验（issue #1）：ok:true 之外还要求 mode=live/snapshot——
  # 曾发生 .env 未跟随 release → 静默回落 mock → 假数据面板照样过检查
  # 重试窗口 60s：新版首次启动可能补建 samples 索引（400 万行），
  # 端口绑定晚于单次检查会被误判失败（2026-10-20 部署实录）
  local attempt
  for attempt in $(seq 1 20); do
    if ssh "${SSH_OPTS[@]}" "$HOST" "curl -fsS --max-time 10 http://127.0.0.1:8898/api/healthz | grep -q '\"ok\":true' && curl -fsS --max-time 10 http://127.0.0.1:8898/api/healthz | grep -qE '\"mode\":\"(live|snapshot)\"'"; then
      echo "✓ 健康检查通过（mode 非 mock，第 ${attempt} 次尝试）"
      return 0
    fi
    sleep 3
  done
  echo "✗ 健康检查失败（60s 未起或回落 mock：检查 /opt/monitor-panel/env 是否存在且含必需凭据）"
  return 1
}

switch_and_restart() {
  local target="$1"
  echo "→ 切换 current → $target"
  ssh "${SSH_OPTS[@]}" "$HOST" "sudo -n ln -sfn '$target' '$CURRENT' && sudo -n systemctl restart monitor-panel && sleep 3"
  health_check
}

if [ "$ROLLBACK" = "1" ]; then
  echo "=== 回滚模式 ==="
  # `|| true` 在远端 shell 内：grep 无匹配时 pipefail 不会冒到远端退出码，
  # 空结果交由下方 -z 判断走明确的 exit 1（否则 set -e 静默退出）
  PREV="$(ssh "${SSH_OPTS[@]}" "$HOST" "ls -1d '$REMOTE_DIR'/releases/*/ 2>/dev/null | grep -vxF \"\$(readlink -f '$CURRENT')/\" | tail -1 || true")"
  if [ -z "$PREV" ]; then
    echo "✗ 没有可回滚的 release"; exit 1
  fi
  echo "→ 目标 release: $PREV"
  switch_and_restart "$PREV" || { echo "✗ 回滚后健康检查失败，请人工介入"; exit 1; }
  echo "=== 回滚完成 ==="
  exit 0
fi

echo "=== 部署 monitor-panel → $HOST:$REMOTE_DIR ==="

echo "→ 备份当前版本（如存在）"
OLD_CURRENT="$(ssh "${SSH_OPTS[@]}" "$HOST" "readlink '$CURRENT' 2>/dev/null || true")"
ssh "${SSH_OPTS[@]}" "$HOST" "mkdir -p '$BACKUPS' '$REMOTE_DIR/releases'"
ssh "${SSH_OPTS[@]}" "$HOST" "if [ -n '$OLD_CURRENT' ] && [ -d '$CURRENT' ]; then
  sudo -n tar czhf '$BACKUPS/pre-$TS.tar.gz' -C '$REMOTE_DIR' --exclude=current/data --exclude=current/.venv --exclude=current/.env current && sudo -n monitor-deploy-ctl chown ubuntu:ubuntu '$BACKUPS/pre-$TS.tar.gz' && echo '✓ 备份 current → pre-'$TS'.tar.gz'
fi"

echo "→ rsync 到 releases/$TS（排除 venv/git/env/数据/node_modules）"
rsync -az --delete -e "ssh -o ConnectTimeout=15 -o ServerAliveInterval=10 -o ServerAliveCountMax=3 -o TCPKeepAlive=yes" \
  --exclude '.venv' --exclude '.git' --exclude '.env' --exclude 'data' \
  --exclude 'backups' --exclude 'releases' --exclude '__pycache__' \
  --exclude 'node_modules' --exclude '.openapi.json' --exclude 'web/dist/.map' \
  --exclude '*.db' --exclude '*.db-wal' --exclude '*.db-shm' \
  "$LOCAL_DIR/" "$HOST:$RELEASE/"

echo "→ 远端依赖准备（uv 优先）"
ssh "${SSH_OPTS[@]}" "$HOST" "cd '$RELEASE' && if command -v uv >/dev/null 2>&1; then uv venv .venv && uv pip install -q -r requirements.txt; else python3 -m venv .venv && '$RELEASE'/.venv/bin/pip install -q -r requirements.txt; fi"

echo "→ release 属主修正为 monitor（服务以 monitor 用户运行；经 sudoers 白名单受控入口）"
ssh "${SSH_OPTS[@]}" "$HOST" "sudo -n monitor-deploy-ctl chown monitor:monitor '$RELEASE'"

# 运行态数据（固定路径，不随 release 切换分叉）：
# - env: 权威源唯一 = /opt/monitor-panel/env（issue #1 统一；systemd 与 config.py
#   双通道都读它，release 内不再拷贝 .env 副本，根除双份漂移）
# - data: 固定 /opt/monitor-panel/data（MONITOR_DB_PATH 指向，避免数据库随 release 分叉）
ssh "${SSH_OPTS[@]}" "$HOST" "sudo -n mkdir -p '$REMOTE_DIR/data' && sudo -n monitor-deploy-ctl chown monitor:monitor '$REMOTE_DIR/data' && \
  if [ ! -f '$REMOTE_DIR/env' ]; then echo '⚠ 警告: $REMOTE_DIR/env 不存在，服务将回落 mock 并被健康检查拦截'; fi"

if switch_and_restart "$RELEASE"; then
  echo "=== 部署成功: $TS ==="
  echo "→ 清理旧 release（保留最近 5 个；审查修复：曾 27 个/1.7G 只增不减）"
  # 旧 release 属主 monitor（部署期 chown），ubuntu 删除前先经受控入口归还属主
  ssh "${SSH_OPTS[@]}" "$HOST" "cd '$REMOTE_DIR/releases' && cur_real=\$(readlink -f '$CURRENT') && \
    ls -1d */ 2>/dev/null | sort -r | tail -n +6 | while read -r r; do \
      p=\$(cd \"\$r\" && pwd); [ \"\$p\" = \"\$cur_real\" ] && continue; \
      sudo -n monitor-deploy-ctl chown ubuntu:ubuntu \"\$p\" && rm -rf \"\$r\" && echo \"  purged \$r\"; done; true"
  echo "  公网验证: curl -fsS https://monitor.dc-sy.cn/api/healthz"
  echo "  回滚:     $0 --rollback --host $HOST --remote-dir $REMOTE_DIR"
else
  echo "✗ 新版本健康检查失败，自动回滚…"
  PREV="$OLD_CURRENT"
  if [ -n "$PREV" ]; then
    switch_and_restart "$PREV" && echo "✓ 已回滚到 $PREV" || echo "✗ 回滚也失败，人工介入"
  fi
  exit 1
fi