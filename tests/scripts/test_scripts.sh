#!/usr/bin/env bash
# ============================================================
# 运维脚本静态断言（审查 P2-19 / P2-20）
# 只做本地静态与最小行为验证；不触碰远端。
# 完整 e2e 回滚（ssh 到部署机切 release + 健康检查）属服务器侧用户参与项。
# ============================================================
set -u

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"

FAILED=0
pass() { echo "ok   - $1"; }
fail() { echo "FAIL - $1"; FAILED=1; }

check() { # check <描述> <命令...>
  local desc="$1"; shift
  if "$@"; then pass "$desc"; else fail "$desc"; fi
}

# --- 1. 语法 ---
for f in scripts/deploy.sh scripts/setup_nginx_cert.sh; do
  check "bash -n $f" bash -n "$f"
done

# --- 2. setup_nginx_cert.sh 生成的 443 配置与现网对齐（SSO 唯一门禁）---
NGINX_SCRIPT="scripts/setup_nginx_cert.sh"

if grep -q 'auth_basic "monitor panel"' "$NGINX_SCRIPT"; then
  fail "nginx 模板不含全站 auth_basic"
else
  pass "nginx 模板不含全站 auth_basic"
fi

if grep -q 'auth_basic_user_file' "$NGINX_SCRIPT"; then
  fail "nginx 模板不含 auth_basic_user_file"
else
  pass "nginx 模板不含 auth_basic_user_file"
fi

ACME_BLOCK="$(awk '/location \^~ \/\.well-known\/acme-challenge\//{f=1} f{print} f && /^    }/{exit}' "$NGINX_SCRIPT")"
if echo "$ACME_BLOCK" | grep -q 'auth_basic off'; then
  pass "ACME 挑战豁免保留 auth_basic off"
else
  fail "ACME 挑战豁免保留 auth_basic off"
fi

INGEST_BLOCK="$(awk '/location = \/api\/ingest/{f=1} f{print} f && /^    }/{exit}' "$NGINX_SCRIPT")"
if echo "$INGEST_BLOCK" | grep -q 'auth_basic off'; then
  pass "ingest 豁免保留 auth_basic off"
else
  fail "ingest 豁免保留 auth_basic off"
fi
if echo "$INGEST_BLOCK" | grep -q 'limit_except POST { deny all; }'; then
  pass "ingest 豁免保留 limit_except POST"
else
  fail "ingest 豁免保留 limit_except POST"
fi

# Basic Auth 生成段与相关变量已随现网移除
if grep -qE 'HTPASSWD|AUTH_USER|htpasswd|openssl passwd -apr1' "$NGINX_SCRIPT"; then
  fail "Basic Auth 生成段/相关变量已清理"
else
  pass "Basic Auth 生成段/相关变量已清理"
fi

# 尾部公网验证不带认证参数
if grep -q 'curl -fsS https://\$DOMAIN/api/healthz' "$NGINX_SCRIPT"; then
  pass "公网验证 curl 不带认证参数"
else
  fail "公网验证 curl 不带认证参数"
fi

# --- 3. deploy/sudoers.deploy 模板 ---
# deploy.sh 公网验证提示不得带 Basic Auth 参数（SSO 唯一门禁，无凭据可给）
if grep -q -- 'curl -fsS -u' scripts/deploy.sh; then
  fail "deploy.sh 不含 curl -fsS -u"
else
  pass "deploy.sh 不含 curl -fsS -u"
fi
if grep -qE '^ubuntu ALL=\(root\) NOPASSWD: /usr/local/sbin/monitor-deploy-ctl$' deploy/sudoers.deploy; then
  pass "sudoers.deploy 含 monitor-deploy-ctl 规则"
else
  fail "sudoers.deploy 含 monitor-deploy-ctl 规则"
fi

# --- 4. backup-db.service 指向 release 内脚本 ---
if grep -qE '^ExecStart=/usr/bin/bash /opt/monitor-panel/current/deploy/backup-db\.sh$' deploy/backup-db.service; then
  pass "backup-db.service ExecStart 指向 current/deploy/backup-db.sh"
else
  fail "backup-db.service ExecStart 指向 current/deploy/backup-db.sh"
fi

# --- 5. 回滚分支：空 releases 时显式 exit 1（而非 pipefail 静默退出）---
# 5a. deploy.sh 的回滚取值为远端 pipefail 兜底
if grep -q 'tail -1 || true' scripts/deploy.sh; then
  pass "deploy.sh 回滚取值在远端 shell 内带 || true"
else
  fail "deploy.sh 回滚取值在远端 shell 内带 || true"
fi

# 5b. 最小复现：空 grep 结果 + || true 后能走到明确的空判断分支
ROLLBACK_OUT="$(bash -c '
set -euo pipefail
PREV="$(ls -1d /nonexistent-releases/*/ 2>/dev/null | grep -vxF /nonexistent-current/ | tail -1 || true)"
if [ -z "$PREV" ]; then echo "NO_RELEASE"; exit 1; fi
exit 0
' 2>&1)" && ROLLBACK_RC=0 || ROLLBACK_RC=$?
if [ "$ROLLBACK_RC" = "1" ] && echo "$ROLLBACK_OUT" | grep -q 'NO_RELEASE'; then
  pass "空 releases 时走显式 exit 1"
else
  fail "空 releases 时走显式 exit 1"
fi

# 5c. deploy.sh 回滚分支本身确有该明确判断
if grep -q '没有可回滚的 release' scripts/deploy.sh; then
  pass "deploy.sh 回滚分支保留空值 exit 1"
else
  fail "deploy.sh 回滚分支保留空值 exit 1"
fi

echo
if [ "$FAILED" = "0" ]; then
  echo "SCRIPT TESTS PASS"
  exit 0
fi
echo "SCRIPT TESTS FAILED"
exit 1
