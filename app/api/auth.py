"""Casdoor SSO OIDC 认证路由：/auth/login, /auth/callback, /auth/logout, /api/me。

- 基于 Authlib 标准 OpenID Connect 流程
- 登录态通过 HTTP-only Session Cookie 持久化
- 兼容反向代理 HTTPS 头（X-Forwarded-Proto）与单点登出（SLO）
"""
from __future__ import annotations

import logging
from typing import Any
from urllib.parse import quote

from authlib.integrations.starlette_client import OAuth
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from app import config

log = logging.getLogger(__name__)

router = APIRouter(tags=["auth"])

oauth = OAuth()
_registered = False


def get_casdoor_client():
    global _registered
    if not _registered:
        if config.CASDOOR_CLIENT_ID and config.CASDOOR_CLIENT_SECRET:
            oauth.register(
                name="casdoor",
                client_id=config.CASDOOR_CLIENT_ID,
                client_secret=config.CASDOOR_CLIENT_SECRET,
                server_metadata_url=f"{config.CASDOOR_ISSUER}/.well-known/openid-configuration",
                client_kwargs={"scope": "openid profile email"},
            )
            _registered = True
    return getattr(oauth, "casdoor", None)


@router.get("/auth/login")
async def login(request: Request):
    """发起 SSO 登录跳转。"""
    client = get_casdoor_client()
    if not client:
        log.warning("SSO client not configured, redirecting to home")
        return RedirectResponse(url="/")

    redirect_uri = str(request.url_for("auth_callback"))
    proto = request.headers.get("x-forwarded-proto", "")
    if proto == "https" or "https://" in config.CASDOOR_ISSUER or "dc-sy.cn" in str(request.base_url):
        redirect_uri = redirect_uri.replace("http://", "https://")

    return await client.authorize_redirect(request, redirect_uri)


@router.get("/auth/callback")
async def auth_callback(request: Request):
    """Casdoor 登录成功回调，换取 Token 并写入 Session。"""
    client = get_casdoor_client()
    if not client:
        return RedirectResponse(url="/")

    try:
        token = await client.authorize_access_token(request)
    except Exception as e:
        log.error("SSO authorization failed: %s", e)
        raise HTTPException(status_code=400, detail=f"SSO 授权失败: {e}")

    user = token.get("userinfo")
    if not user:
        # 尝试解析 id_token
        try:
            user = await client.parse_id_token(request, token)
        except Exception:
            user = {}

    if not user:
        raise HTTPException(status_code=400, detail="未能获取用户信息")

    username = user.get("name") or user.get("preferred_username") or user.get("sub", "User")
    request.session["user"] = {
        "id": user.get("sub"),
        "name": username,
        "email": user.get("email", ""),
        "avatar": user.get("picture") or user.get("avatar", ""),
    }
    log.info("SSO user logged in: %s (%s)", username, user.get("sub"))
    return RedirectResponse(url="/")


@router.get("/auth/logout")
async def logout(request: Request):
    """单点退出：清空本地 Session 并重定向至 Casdoor 退出端点。"""
    request.session.clear()
    home_url = "https://monitor.dc-sy.cn/" if "dc-sy.cn" in str(request.base_url) else str(request.base_url)
    logout_url = f"{config.CASDOOR_ISSUER}/api/logout?redirect_uri={quote(home_url, safe='')}"
    return RedirectResponse(url=logout_url)


@router.get("/api/me")
async def me(request: Request):
    """获取当前已登录用户信息（前端调用）。"""
    user = request.session.get("user")
    if not user:
        return {"ok": False, "error": "unauthorized", "data": None}
    return {"ok": True, "data": user}
