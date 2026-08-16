#!/usr/bin/env bash
# ============================================================
# monitor-panel 原子部署脚本（M4）
#   本地 → rsync 到 releases/<ts> → 备份 → 符号链接切换 →
#   systemctl restart → 健康检查 → 失败自动回滚
# 用法:
#   ./scripts/deploy.sh                      # 部署当前代码
#   ./scripts/deploy.sh --rollback           # 回滚到上一 release
#   ./scripts/deploy.sh --host dc-sy --remote-dir /opt/monitor-panel
# 注意: 健康检查走本机 127.0.0.1（N2：绕开 nginx Basic Auth）
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

health_check() {
  echo "→ 健康检查 (127.0.0.1:8898/api/healthz)…"
  if ssh "$HOST" "curl -fsS --max-time 10 http://127.0.0.1:8898/api/healthz | grep -q '\\"ok\\":true'"; then
    echo "✓ 健康检查通过"
    return 0
  fi
  echo "✗ 健康检查失败"
  return 1
}

switch_and_restart() {
  local target="$1"
  echo "→ 切换 current → $target"
  ssh "$HOST" "ln -sfn '$target' '$CURRENT' && systemctl restart monitor-panel && sleep 3"
  health_check
}

if [ "$ROLLBACK" = "1" ]; then
  echo "=== 回滚模式 ==="
  PREV="$(ssh "$HOST" "ls -1d '$REMOTE_DIR'/releases/*/ 2>/dev/null | grep -v -F \$(readlink '$CURRENT') | tail -1")"
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
ssh "$HOST" "mkdir -p '$BACKUPS' '$REMOTE_DIR/releases'"
ssh "$HOST" "if [ -e '$CURRENT' ] && [ -d '$CURRENT' ]; then
  tar czf '$BACKUPS/pre-$TS.tar.gz' -C '$REMOTE_DIR' --exclude=current/data --exclude=current/.venv --exclude=current/.env current 2>/dev/null && echo '✓ 备份 current → pre-'$TS'.tar.gz'
fi"

echo "→ rsync 到 releases/$TS（排除 venv/git/env/数据）"
rsync -az --delete \
  --exclude '.venv' --exclude '.git' --exclude '.env' --exclude 'data' \
  --exclude 'backups' --exclude 'releases' --exclude '__pycache__' \
  --exclude '*.db' --exclude '*.db-wal' --exclude '*.db-shm' \
  "$LOCAL_DIR/" "$HOST:$RELEASE/"

echo "→ 远端依赖准备（uv 优先）"
ssh "$HOST" "cd '$RELEASE' && if command -v uv >/dev/null 2>&1; then uv venv .venv && uv pip install -q -r requirements.txt; else python3 -m venv .venv && '$RELEASE'/.venv/bin/pip install -q -r requirements.txt; fi"

# 运行态配置与数据（固定路径，不随 release 切换分叉）：
# - .env: 复制到 release 内（代码读 BASE_DIR/.env），权威源 /opt/monitor-panel/env（systemd）
# - data: 固定 /opt/monitor-panel/data（MONITOR_DB_PATH 指向，避免数据库随 release 分叉）
ssh "$HOST" "sudo mkdir -p '$REMOTE_DIR/data' && sudo chown monitor:monitor '$REMOTE_DIR/data' && \
  if [ -f '$REMOTE_DIR/env' ]; then sudo cp '$REMOTE_DIR/env' '$RELEASE/.env'; \
  sudo chown monitor:monitor '$RELEASE/.env'; sudo chmod 600 '$RELEASE/.env'; fi"

if switch_and_restart "$RELEASE"; then
  echo "=== 部署成功: $TS ==="
  echo "  公网验证: curl -fsS -u <user> https://monitor.dc-sy.cn/api/healthz"
  echo "  回滚:     $0 --rollback --host $HOST --remote-dir $REMOTE_DIR"
else
  echo "✗ 新版本健康检查失败，自动回滚…"
  PREV="$(ssh "$HOST" "ls -1d '$REMOTE_DIR'/releases/*/ 2>/dev/null | grep -v -F '$RELEASE' | tail -1")"
  if [ -n "$PREV" ]; then
    switch_and_restart "$PREV" && echo "✓ 已回滚到 $PREV" || echo "✗ 回滚也失败，人工介入"
  fi
  exit 1
fi