#!/usr/bin/env bash
# 每日 SQLite 备份（WAL 安全）——backup-db.service 调用。
# 2026-08-24 修复：服务器无 sqlite3 CLI（unit 曾每日 exit 127），
# 改用 python3 标准库 sqlite3.Connection.backup()（等价 WAL 安全 .backup，零依赖）。
set -euo pipefail

DB="/opt/monitor-panel/data/monitor.db"
DIR="/opt/monitor-panel/backups"
OUT="$DIR/monitor-$(date +%F).db"

mkdir -p "$DIR"
/usr/bin/python3 - "$DB" "$OUT" <<'EOF'
import sqlite3
import sys

src = sqlite3.connect(sys.argv[1])
dst = sqlite3.connect(sys.argv[2])
src.backup(dst)
dst.close()
src.close()
print("backup ok:", sys.argv[2])
EOF
/usr/bin/find "$DIR" -maxdepth 1 -name "monitor-*.db" -mtime +14 -delete
