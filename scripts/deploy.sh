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
  # 双重校验（issue #1）：ok:true 之外还要求 mode=live/snapshot——
  # 曾发生 .env 未跟随 release → 静默回落 mock → 假数据面板照样过检查
  if ssh "$HOST" "curl -fsS --max-time 10 http://127.0.0.1:8898/api/healthz | grep -q '\"ok\":true' && curl -fsS --max-time 10 http://127.0.0.1:8898/api/healthz | grep -qE '\"mode\":\"(live|snapshot)\"'"; then
    echo "✓ 健康检查通过（mode 非 mock）"
    return 0
  fi
  echo "✗ 健康检查失败（服务未起或回落 mock：检查 /opt/monitor-panel/env 是否存在且含必需凭据）"
  return 1
}

switch_and_restart() {
  local target="$1"
  echo "→ 切换 current → $target"
  ssh "$HOST" "sudo ln -sfn '$target' '$CURRENT' && sudo systemctl restart monitor-panel && sleep 3"
  health_check
}

if [ "$ROLLBACK" = "1" ]; then
  echo "=== 回滚模式 ==="
  PREV="$(ssh "$HOST" "ls -1d '$REMOTE_DIR'/releases/*/ 2>/dev/null | grep -vxF \"\$(readlink -f '$CURRENT')/\" | tail -1")"
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
OLD_CURRENT="$(ssh "$HOST" "readlink '$CURRENT' 2>/dev/null || true")"
ssh "$HOST" "mkdir -p '$BACKUPS' '$REMOTE_DIR/releases'"
ssh "$HOST" "if [ -n '$OLD_CURRENT' ] && [ -d '$CURRENT' ]; then
  sudo tar czhf '$BACKUPS/pre-$TS.tar.gz' -C '$REMOTE_DIR' --exclude=current/data --exclude=current/.venv --exclude=current/.env current && sudo -n monitor-deploy-ctl chown ubuntu:ubuntu '$BACKUPS/pre-$TS.tar.gz' && echo '✓ 备份 current → pre-'$TS'.tar.gz'
fi"

echo "→ rsync 到 releases/$TS（排除 venv/git/env/数据/node_modules）"
rsync -az --delete \
  --exclude '.venv' --exclude '.git' --exclude '.env' --exclude 'data' \
  --exclude 'backups' --exclude 'releases' --exclude '__pycache__' \
  --exclude 'node_modules' --exclude '.openapi.json' --exclude 'web/dist/.map' \
  --exclude '*.db' --exclude '*.db-wal' --exclude '*.db-shm' \
  "$LOCAL_DIR/" "$HOST:$RELEASE/"

echo "→ 远端依赖准备（uv 优先）"
ssh "$HOST" "cd '$RELEASE' && if command -v uv >/dev/null 2>&1; then uv venv .venv && uv pip install -q -r requirements.txt; else python3 -m venv .venv && '$RELEASE'/.venv/bin/pip install -q -r requirements.txt; fi"

echo "→ release 属主修正为 monitor（服务以 monitor 用户运行；经 sudoers 白名单受控入口）"
ssh "$HOST" "sudo -n monitor-deploy-ctl chown monitor:monitor '$RELEASE'"

# 运行态数据（固定路径，不随 release 切换分叉）：
# - env: 权威源唯一 = /opt/monitor-panel/env（issue #1 统一；systemd 与 config.py
#   双通道都读它，release 内不再拷贝 .env 副本，根除双份漂移）
# - data: 固定 /opt/monitor-panel/data（MONITOR_DB_PATH 指向，避免数据库随 release 分叉）
ssh "$HOST" "sudo -n mkdir -p '$REMOTE_DIR/data' && sudo -n monitor-deploy-ctl chown monitor:monitor '$REMOTE_DIR/data' && \
  if [ ! -f '$REMOTE_DIR/env' ]; then echo '⚠ 警告: $REMOTE_DIR/env 不存在，服务将回落 mock 并被健康检查拦截'; fi"

if switch_and_restart "$RELEASE"; then
  echo "=== 部署成功: $TS ==="
  echo "→ 清理旧 release（保留最近 5 个；审查修复：曾 27 个/1.7G 只增不减）"
  # 旧 release 属主 monitor（部署期 chown），ubuntu 删除前先经受控入口归还属主
  ssh "$HOST" "cd '$REMOTE_DIR/releases' && cur_real=\$(readlink -f '$CURRENT') && \
    ls -1d */ 2>/dev/null | sort -r | tail -n +6 | while read -r r; do \
      p=\$(cd \"\$r\" && pwd); [ \"\$p\" = \"\$cur_real\" ] && continue; \
      sudo -n monitor-deploy-ctl chown ubuntu:ubuntu \"\$p\" && rm -rf \"\$r\" && echo \"  purged \$r\"; done; true"
  echo "  公网验证: curl -fsS -u <user> https://monitor.dc-sy.cn/api/healthz"
  echo "  回滚:     $0 --rollback --host $HOST --remote-dir $REMOTE_DIR"
else
  echo "✗ 新版本健康检查失败，自动回滚…"
  PREV="$OLD_CURRENT"
  if [ -n "$PREV" ]; then
    switch_and_restart "$PREV" && echo "✓ 已回滚到 $PREV" || echo "✗ 回滚也失败，人工介入"
  fi
  exit 1
fi