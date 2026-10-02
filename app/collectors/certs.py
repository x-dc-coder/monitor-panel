"""SSL 证书到期扫描（F5，Phase 1 重构）。

三层探测源（优先级递减，结果互补）：
1. TLS SNI 主动握手：对各域名 443 端口发起异步握手，取对端证书 DER，
   cryptography.x509 解析 SAN 全列表 / Issuer / not_before / not_after /
   链状态——免 root/sudo，覆盖 nginx 分发的外部视角（用户实际拿到什么）
2. nginx 动态发现：`nginx -T` dump 中解析 ssl_certificate 指令，纳入
   未在 CERT_DOMAINS 登记的证书目录（live 模式）
3. 本地文件回退：/etc/letsencrypt/live/<domain>/cert.pem 经 sudoers 白名单
   openssl 读取（TLS 握手不可达但证书即将部署/续期时兜底）

- 无效/不可读目录记日志跳过，不崩整轮（评审 E16）
- 告警生命周期由聚合层 `_on_cert_days` 闭环：正常 resolve_alerts(cert:<domain>)
"""
from __future__ import annotations

import asyncio
import logging
import re
import ssl
import time
from datetime import datetime, timezone

from app import config, constants
from app.collectors.base import Collector, CollectorError, CollectorResult, run_cmd

log = logging.getLogger(__name__)

_ENDDATE_RE = re.compile(r"notAfter=(\w{3}\s+\d+\s+[\d:]+\s+\d+\s+\w{3})")
_NGINX_CERT_RE = re.compile(r"^\s*ssl_certificate\s+(\S+\.pem);", re.M)
_TLS_TIMEOUT = 8.0
# 域名合法性（SNI 防注入；_ 不出现在证书 CN 但 tailscale MagicDNS 会用到）
_DOMAIN_RE = re.compile(r"^[A-Za-z0-9_.-]+$")

_CORE_DOMAINS = [
    "dc-sy.cn",
    "monitor.dc-sy.cn",
    "headscale.dc-sy.cn",
    "sso.dc-sy.cn",
    "spark.dc-sy.cn",
    "releases.monitor.dc-sy.cn",
    "cloudreve.dc-sy.cn",
]


def cert_status(days_left: float) -> str:
    if days_left < config.THRESHOLDS["cert_crit_days"]:
        return constants.LEVEL_CRIT
    if days_left < config.THRESHOLDS["cert_warn_days"]:
        return constants.LEVEL_WARN
    return "ok"


def _fmt_dt(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _days_left(not_after: datetime) -> float:
    return (not_after - datetime.now(timezone.utc)).total_seconds() / 86400


class CertsCollector(Collector):
    name = "certs"
    group = constants.GROUP_CERT_DAYS
    interval = 3600

    def __init__(self, mock: bool | None = None) -> None:
        super().__init__(mock)
        self._sem: asyncio.Semaphore | None = None

    def _semaphore(self) -> asyncio.Semaphore:
        if self._sem is None:
            self._sem = asyncio.Semaphore(8)   # 并发握手限流，防 DNS/网络抖动放大
        return self._sem

    # ---------------- mock ----------------
    def _mock_result(self) -> CollectorResult:
        ts = time.time()
        result = CollectorResult(node_id=config.NODE_ID, group=self.group)
        certs = [
            {"domain": "monitor.dc-sy.cn", "days_left": 89.0, "not_after": "2026-11-12",
             "status": "ok"},
            {"domain": "dc-sy.cn", "days_left": 45.0, "not_after": "2026-09-29",
             "status": "ok"},
            {"domain": "headscale.dc-sy.cn", "days_left": 12.0, "not_after": "2026-08-27",
             "status": "warn"},
            {"domain": "legacy.frp.example", "days_left": 4.0, "not_after": "2026-08-19",
             "status": "crit"},
        ]
        for c in certs:
            result.add(f"days_left.{c['domain']}", c["days_left"], ts)
        result.payload = {"certs": certs, "ts": ts, "mock": True}
        return result

    # ---------------- real：TLS SNI 主动探测 ----------------
    async def _probe_tls(self, domain: str) -> dict | None:
        """443 端口 TLS 握手取证书（DER → x509 解析），免 root。

        返回 None 表示握手失败（域名不解析/端口未开/TLS 错误），由上层回退
        本地文件读取或记 error。
        """
        if not _DOMAIN_RE.fullmatch(domain):
            log.warning("certs: skip invalid domain %r", domain)
            return None
        ctx = ssl.create_default_context()
        # 链校验结果记录进 chain_verified；证书本身仍可解析（自签/过期也要报）
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        try:
            async with self._semaphore():
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(domain, 443, ssl=ctx,
                                            server_hostname=domain),
                    timeout=_TLS_TIMEOUT)
        except (OSError, asyncio.TimeoutError) as exc:
            log.info("certs: TLS probe %s failed: %s", domain, exc)
            return None
        try:
            tls = writer.get_extra_info("ssl_object")
            if tls is None:
                return None
            der = tls.getpeercert(binary_form=True)
            chain_verified = False
            # 链校验：用校验上下文再取 verified 链（Python ≥3.10 提供
            # get_verified_chain；不可用时退化为 CERT_REQUIRED 试握手成功与否）
            try:
                vctx = ssl.create_default_context()
                vreader, vwriter = await asyncio.wait_for(
                    asyncio.open_connection(domain, 443, ssl=vctx,
                                            server_hostname=domain),
                    timeout=_TLS_TIMEOUT)
                chain_verified = True
                vwriter.close()
                try:
                    await vwriter.wait_closed()
                except (OSError, asyncio.TimeoutError):
                    pass
            except (OSError, ssl.SSLCertVerificationError, asyncio.TimeoutError):
                chain_verified = False
            return self._parse_der(domain, der, chain_verified)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (OSError, asyncio.TimeoutError):
                pass

    @staticmethod
    def _parse_der(domain: str, der: bytes, chain_verified: bool) -> dict | None:
        try:
            from cryptography import x509
            cert = x509.load_der_x509_certificate(der)
        except Exception as exc:  # noqa: BLE001 —— 解析失败按探测失败处理
            log.warning("certs: x509 parse failed for %s: %s", domain, exc)
            return None
        try:
            san_ext = cert.extensions.get_extension_for_class(
                x509.SubjectAlternativeName)
            sans = san_ext.value.get_values_for_type(x509.DNSName)
        except x509.ExtensionNotFound:
            sans = []
        not_after = cert.not_valid_after_utc
        not_before = cert.not_valid_before_utc
        days = _days_left(not_after)
        return {
            "domain": domain,
            "days_left": round(days, 1),
            "not_after": _fmt_dt(not_after),
            "not_before": _fmt_dt(not_before),
            "issuer": cert.issuer.rfc4514_string(),
            "subject": cert.subject.rfc4514_string(),
            "sans": sans,
            "chain_verified": chain_verified,
            "source": "tls",
            "status": cert_status(days),
        }

    # ---------------- real：nginx 动态发现 ----------------
    async def _nginx_discover(self) -> list[str]:
        """解析 ssl_certificate 指令 → 证书目录名即域名。

        先读 /etc/nginx/sites-enabled 与 sites-available，再回退尝试 nginx -T。
        nginx 必须以绝对路径 /usr/sbin/nginx 调用：sudoers 白名单按命令逐字匹配
        （deploy/sudoers.monitor），相对路径解析结果不匹配白名单 → sudo 要求密码
        → sudo -n 认证失败（PAM error 日志 → 触发 R1 日志告警，2026-10-02 复盘）。
        """
        from pathlib import Path

        domains: list[str] = []
        for conf_dir in [Path("/etc/nginx/sites-enabled"), Path("/etc/nginx/sites-available")]:
            if not conf_dir.is_dir():
                continue
            try:
                for conf_file in conf_dir.glob("*"):
                    if not conf_file.is_file():
                        continue
                    try:
                        text = conf_file.read_text(encoding="utf-8", errors="ignore")
                        for match in _NGINX_CERT_RE.finditer(text):
                            parts = match.group(1).strip('"').split("/")
                            if len(parts) >= 2 and parts[-1].endswith(".pem"):
                                domain = parts[-2]
                                if _DOMAIN_RE.fullmatch(domain) and domain not in domains:
                                    domains.append(domain)
                    except Exception:
                        pass
            except Exception:
                pass

        try:
            # 绝对路径：须与 sudoers 白名单逐字匹配（/usr/sbin/nginx -T）
            rc, out, err = await run_cmd(["/usr/sbin/nginx", "-T"], sudo=config.SUDO,
                                         timeout=config.STEP_TIMEOUT)
            if rc == 0:
                for match in _NGINX_CERT_RE.finditer(out):
                    parts = match.group(1).strip('"').split("/")
                    if len(parts) >= 2 and parts[-1].endswith(".pem"):
                        domain = parts[-2]
                        if _DOMAIN_RE.fullmatch(domain) and domain not in domains:
                            domains.append(domain)
            else:
                log.warning("certs: nginx -T rc=%d（动态域名发现跳过）: %s",
                            rc, err.strip()[:120])
        except CollectorError as exc:
            log.warning("certs: nginx -T 调用失败（动态域名发现跳过）: %s", exc)
        return domains

    # ---------------- real：本地文件回退 ----------------
    async def _read_cert(self, domain: str) -> dict | None:
        path = config.LETSENCRYPT_LIVE / domain / "cert.pem"
        try:
            rc, out, err = await run_cmd(
                ["openssl", "x509", "-in", str(path), "-noout", "-enddate", "-startdate",
                 "-issuer"],
                sudo=config.SUDO, timeout=config.STEP_TIMEOUT)
        except CollectorError as exc:
            log.warning("certs: read %s failed: %s", path, exc)
            return None
        if rc != 0:
            # 证书不存在/不可达（目录 700 root only，monitor 无法 stat）
            log.warning("certs: openssl %s rc=%d: %s", path, rc, err.strip()[:120])
            return None
        m = _ENDDATE_RE.search(out)
        if not m:
            log.warning("certs: no notAfter in output for %s", path)
            return None
        not_after = m.group(1)
        try:
            # openssl 默认输出：'Nov  2 14:47:45 2026 GMT'（非 ISO）
            dt = datetime.strptime(not_after, "%b %d %H:%M:%S %Y %Z")
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            days_left = _days_left(dt)
        except ValueError:
            try:
                dt = datetime.fromisoformat(not_after.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                days_left = _days_left(dt)
            except ValueError:
                log.warning("certs: bad date %r for %s", not_after, path)
                return None
        issuer = ""
        m2 = re.search(r"issuer=(.+)", out)
        if m2:
            issuer = m2.group(1).strip()
        return {"domain": domain, "days_left": round(days_left, 1),
                "not_after": not_after, "issuer": issuer or None,
                "chain_verified": None, "source": "file",
                "status": cert_status(days_left), "path": str(path)}

    # ---------------- real ----------------
    async def _run_real(self) -> CollectorResult:
        ts = time.time()
        result = CollectorResult(node_id=config.NODE_ID, group=self.group)
        domains = list(config.CERT_DOMAINS)
        for core in _CORE_DOMAINS:
            if core not in domains:
                domains.append(core)
        for d in await self._nginx_discover():
            if d not in domains:
                domains.append(d)
        certs: list[dict] = []
        seen: set[str] = set()
        # TLS 探测并发跑（外部视角优先）
        probed = await asyncio.gather(*[self._probe_tls(d) for d in domains])
        for domain, c in zip(domains, probed):
            if c:
                certs.append(c)
                seen.add(domain)
                result.add(f"days_left.{domain}", c["days_left"], ts)
        # 回退：TLS 不可达的域名走本地文件（记录探测异常）
        for domain, c in zip(domains, probed):
            if c is None and domain not in seen:
                fc = await self._read_cert(domain)
                if fc:
                    certs.append(fc)
                    seen.add(domain)
                    result.add(f"days_left.{domain}", fc["days_left"], ts)
        for domain in domains:
            if domain not in seen:
                result.add(f"days_left.{domain}", -1.0, ts)
                certs.append({"domain": domain, "days_left": -1.0, "status":
                              constants.LEVEL_CRIT, "source": "none",
                              "error": "TLS 握手失败且本地证书不可读"})
        result.payload = {"certs": certs, "ts": ts}
        return result

    async def _mock_run(self) -> CollectorResult:
        return self._mock_result()
