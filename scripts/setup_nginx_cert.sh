#!/usr/bin/env bash
# ============================================================
# monitor.dc-sy.cn nginx server block + Basic Auth + 双通道证书签发（M4）
# 用法:
#   ./scripts/setup_nginx_cert.sh --issuance http   # certbot HTTP-01（已备案/未被 webblock）
#   ./scripts/setup_nginx_cert.sh --issuance dns    # acme.sh dns_ali DNS-01（阿里云 AK，推荐兜底）
#   ./scripts/setup_nginx_cert.sh --no-cert         # 只生成 nginx block + Basic Auth，不签发
# 环境: Ali_Key / Ali_Secret（dns 通道必需，RAM 子账号仅 DNS 解析权限）
# 硬性要点:
#   - N1 修正: location ^~ /.well-known/acme-challenge/ { auth_basic off; }
#   - 预检 8a: 外部网络 curl -sI 探测 webblock，401/403 = Basic Auth 干扰先补豁免再判
#   - 续期与签发方式一致（http=certbot timer；dns=acme.sh --cron）
# ============================================================
set -euo pipefail

DOMAIN="monitor.dc-sy.cn"
BACKEND="127.0.0.1:8898"
ISSUANCE=""
NO_CERT=0
AUTH_USER="monitor"
HTPASSWD="/etc/nginx/.monitor-htpasswd"
SITE="/etc/nginx/sites-available/$DOMAIN"
LINK="/etc/nginx/sites-enabled/$DOMAIN"

while [ $# -gt 0 ]; do
  case "$1" in
    --issuance) ISSUANCE="$2"; shift 2 ;;
    --no-cert) NO_CERT=1; shift ;;
    --domain) DOMAIN="$2"; shift 2 ;;
    *) echo "未知参数: $1"; exit 2 ;;
  esac
done
if [ "$NO_CERT" != "1" ] && [ "$ISSUANCE" != "http" ] && [ "$ISSUANCE" != "dns" ]; then
  echo "✗ 必须指定 --issuance http|dns（或 --no-cert）"; exit 2
fi

echo "=== 1/5 Basic Auth（默认开启，可关——评审 C2）==="
if [ ! -f "$HTPASSWD" ]; then
  PASS="$(openssl rand -base64 18 | tr -d '/+=' | head -c 14)"
  HASH="$(openssl passwd -apr1 "$PASS")"
  echo "$AUTH_USER:$HASH" > "$HTPASSWD"
  chmod 600 "$HTPASSWD"
  echo "✓ 已生成 $HTPASSWD（用户 $AUTH_USER，密码: $PASS —— 请立即保存并删除本行记录）"
else
  echo "✓ 已存在 $HTPASSWD，跳过"
fi

echo "=== 2/5 生成 nginx server block ==="
cat > "$SITE" <<EOF
# $DOMAIN → $BACKEND（monitor-panel；由 scripts/setup_nginx_cert.sh 生成）
# Basic Auth 默认开启；ACME 挑战豁免（N1 修正）
server {
    listen 80;
    listen [::]:80;
    server_name $DOMAIN;

    # ACME HTTP-01 挑战豁免 Basic Auth（N1：certbot 请求不带凭据）
    location ^~ /.well-known/acme-challenge/ {
        auth_basic off;
        root /var/www/html;
    }

    location / {
        return 301 https://\$host\$request_uri;
    }
}

server {
    listen 443 ssl;
    listen [::]:443 ssl;
    http2 on;
    server_name $DOMAIN;

    ssl_certificate     /etc/letsencrypt/live/$DOMAIN/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/$DOMAIN/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;

    auth_basic "monitor panel";
    auth_basic_user_file $HTPASSWD;

    # ACME 挑战豁免（HTTP-01 续期需要；DNS 通道时亦保留，为将来迁移留路）
    location ^~ /.well-known/acme-challenge/ {
        auth_basic off;
        root /var/www/html;
    }

    location / {
        proxy_pass http://$BACKEND;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 30s;
    }
}
EOF
ln -sfn "$SITE" "$LINK"
nginx -t
systemctl reload nginx
echo "✓ nginx block 已生效（Basic Auth 默认开启）"

if [ "$NO_CERT" = "1" ]; then
  echo "=== 完成（--no-cert，未签发证书；443 暂时不可用）==="
  exit 0
fi

echo "=== 3/5 预检 8a（webblock 探测；从外部网络执行更准确）==="
PROBE_CODE="$(curl -sI --max-time 10 "http://$DOMAIN/.well-known/acme-challenge/probe" | head -1 || true)"
echo "  HTTP 探测结果: $PROBE_CODE"
if echo "$PROBE_CODE" | grep -qiE 'webblock|qcloud|tencent'; then
  echo "✗ 检测到腾讯云 webblock 拦截 → 必须走 dns 通道"
  if [ "$ISSUANCE" = "http" ]; then echo "  请改用 --issuance dns"; exit 1; fi
elif echo "$PROBE_CODE" | grep -qE '40[13]'; then
  echo "! 401/403 = Basic Auth 干扰，先补豁免再判（N1）；已含 acme-challenge 豁免，继续"
fi

echo "=== 4/5 证书签发（$ISSUANCE 通道）==="
if [ "$ISSUANCE" = "http" ]; then
  certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos --redirect
  echo "✓ certbot HTTP-01 签发完成；续期由 certbot systemd timer 自动处理"
else
  command -v acme.sh >/dev/null || curl -sL https://get.acme.sh | sh
  :"${Ali_Key:?dns 通道需要 Ali_Key 环境变量（阿里云 RAM 子账号，仅 DNS 权限）}"
  :"${Ali_Secret:?dns 通道需要 Ali_Secret 环境变量}"
  export Ali_Key Ali_Secret
  acme.sh --issue --dns dns_ali -d "$DOMAIN" --server letsencrypt
  acme.sh --install-cert -d "$DOMAIN" \
    --key-file /etc/letsencrypt/live/$DOMAIN/privkey.pem \
    --fullchain-file /etc/letsencrypt/live/$DOMAIN/fullchain.pem \
    --reloadcmd "nginx -s reload"
  acme.sh --install-cronjob 2>/dev/null || true
  echo "✓ acme.sh dns_ali DNS-01 签发完成；续期由 acme.sh --cron 自动处理（复用同一凭据）"
  echo "  凭据安全: Ali_Key/Ali_Secret 环境注入，勿写入仓库；凭据文件权限 600"
fi

echo "=== 5/5 上线验证 ==="
nginx -t && systemctl reload nginx
echo "  本机验证（绕开 Basic Auth）: curl -fsS http://127.0.0.1:8898/api/healthz"
echo "  公网验证: curl -fsS -u $AUTH_USER https://$DOMAIN/api/healthz"
echo "=== setup_nginx_cert.sh 完成 ==="