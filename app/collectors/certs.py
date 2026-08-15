"""SSL 证书到期扫描（F5）。

- /etc/letsencrypt/live 目录权限 700（root only）→ 经 sudoers 白名单
  openssl 读取（deploy/sudoers.monitor）
- 无效/不可读目录记日志跳过，不崩整轮（评审 E16）
"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone

from app import config, constants
from app.collectors.base import Collector, CollectorError, CollectorResult, run_cmd

log = logging.getLogger(__name__)

_ENDDATE_RE = re.compile(r"notAfter=(S+)")


def cert_status(days_left: float) -> str:
    if days_left < config.THRESHOLDS["cert_crit_days"]:
        return constants.LEVEL_CRIT
    if days_left < config.THRESHOLDS["cert_warn_days"]:
        return constants.LEVEL_WARN
    return "ok"


class CertsCollector(Collector):
    name = "certs"
    group = constants.GROUP_CERT_DAYS
    interval = 3600

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

    # ---------------- real ----------------
    async def _read_cert(self, domain: str) -> dict | None:
        path = config.LETSENCRYPT_LIVE / domain / "cert.pem"
        if not path.exists():
            log.warning("certs: %s missing, skip", path)
            return None
        try:
            rc, out, err = await run_cmd(
                ["openssl", "x509", "-in", str(path), "-noout", "-enddate"],
                sudo=config.SUDO, timeout=config.STEP_TIMEOUT)
        except CollectorError as exc:
            log.warning("certs: read %s failed: %s", path, exc)
            return None
        if rc != 0:
            log.warning("certs: openssl %s rc=%d: %s", path, rc, err.strip()[:120])
            return None
        m = _ENDDATE_RE.search(out)
        if not m:
            log.warning("certs: no notAfter in output for %s", path)
            return None
        not_after = m.group(1)
        try:
            dt = datetime.fromisoformat(not_after.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            days_left = (dt - datetime.now(timezone.utc)).total_seconds() / 86400
        except ValueError:
            log.warning("certs: bad date %r for %s", not_after, path)
            return None
        return {"domain": domain, "days_left": round(days_left, 1),
                "not_after": not_after, "status": cert_status(days_left),
                "path": str(path)}

    async def _run_real(self) -> CollectorResult:
        ts = time.time()
        result = CollectorResult(node_id=config.NODE_ID, group=self.group)
        certs: list[dict] = []
        for domain in config.CERT_DOMAINS:
            c = await self._read_cert(domain)
            if c:
                certs.append(c)
                result.add(f"days_left.{domain}", c["days_left"], ts)
        result.payload = {"certs": certs, "ts": ts}
        return result

    async def _mock_run(self) -> CollectorResult:
        return self._mock_result()
