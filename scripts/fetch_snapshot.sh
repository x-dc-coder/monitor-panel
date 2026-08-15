#!/usr/bin/env bash
# ============================================================
# 抓取 cloud 真实数据快照 → 本地 snapshots/（MONITOR_MOCK=snapshot 读取）
# 用法: ./scripts/fetch_snapshot.sh [ssh-host]   # 默认 dc-sy
# 说明: 远程脚本 fetch_snapshot_remote.py 在 cloud 上执行（纯标准库，
#       只读抓取 frps dashboard / 8899 / headscale / tailscale / certs /
#       system / network），产物 scp 回本地。凭据在远程脚本头部维护。
# ============================================================
set -euo pipefail
HOST="${1:-dc-sy}"
LOCAL_SNAP="$(cd "$(dirname "$0")/.." && pwd)/snapshots"
REMOTE_SCRIPT="$(dirname "$0")/fetch_snapshot_remote.py"

mkdir -p "$LOCAL_SNAP"
ssh "$HOST" "rm -rf /tmp/monitor-snapshots && mkdir -p /tmp/monitor-snapshots"
cat "$REMOTE_SCRIPT" | ssh "$HOST" "python3 -"
scp -q "$HOST:/tmp/monitor-snapshots/frp.json" \
         "$HOST:/tmp/monitor-snapshots/tailscale.json" \
         "$HOST:/tmp/monitor-snapshots/headscale.json" \
         "$HOST:/tmp/monitor-snapshots/system.json" \
         "$HOST:/tmp/monitor-snapshots/network.json" \
         "$HOST:/tmp/monitor-snapshots/certs.json" "$LOCAL_SNAP/"
echo "✓ snapshots → $LOCAL_SNAP"
ls -la "$LOCAL_SNAP"
