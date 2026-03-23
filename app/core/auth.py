"""OIDC authentication — handles the full OAuth 2.0 flow in-app.

Login → OIDC provider authorization → callback (code exchange) → session cookie.
Session cookie holds the access token (JWT), verified on each request.
"""

import os
import secrets
from pathlib import Path

import httpx
import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import SecurityScopes
from itsdangerous import BadSignature, TimestampSigner
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.database import get_db
from app.models.fa_case import FAUser

_ALGORITHM = "RS256"
SESSION_COOKIE = "_qvault_session"

# Cache public key with mtime check so key rotation takes effect without restart.
_pk_cache: tuple[float, str] = (0.0, "")

# Cache OIDC discovery document.
_oidc_config: dict | None = None


# ── OIDC Discovery ──────────────────────────────────────────────


async def _get_oidc_config() -> dict:
    """Fetch and cache OIDC well-known configuration."""
    global _oidc_config
    if _oidc_config is not None:
        return _oidc_config

    url = settings.oidc_issuer_url.rstrip("/") + "/.well-known/openid-configuration"
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, timeout=10)
        resp.raise_for_status()
        _oidc_config = resp.json()
    logger.info("OIDC discovery loaded from {}", url)
    return _oidc_config


def get_authorization_url(state: str) -> str:
    """Build the OIDC authorization URL (called synchronously after discovery)."""
    if _oidc_config is None:
        raise RuntimeError("OIDC config not loaded; call _get_oidc_config() first")
    auth_endpoint = _oidc_config["authorization_endpoint"]
    params = httpx.QueryParams(
        {
            "response_type": "code",
            "client_id": settings.oauth2_client_id,
            "redirect_uri": settings.oauth2_redirect_url,
            "scope": "openid profile",
            "state": state,
        }
    )
    return f"{auth_endpoint}?{params}"


async def exchange_code(code: str) -> dict:
    """Exchange authorization code for tokens via the OIDC token endpoint."""
    oidc = await _get_oidc_config()
    token_endpoint = oidc["token_endpoint"]
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            token_endpoint,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": settings.oauth2_redirect_url,
                "client_id": settings.oauth2_client_id,
                "client_secret": settings.oauth2_client_secret,
            },
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()


# ── Session Cookie ──────────────────────────────────────────────


def _signer() -> TimestampSigner:
    return TimestampSigner(settings.session_secret)


def create_session_cookie(access_token: str) -> str:
    """Sign the access token for storage in a cookie."""
    return _signer().sign(access_token).decode()


def read_session_cookie(value: str) -> str | None:
    """Unsign and return the access token, or None if invalid/expired."""
    try:
        # max_age=24h — cookie is re-signed on each login
        return _signer().unsign(value, max_age=86400).decode()
    except BadSignature:
        return None


# ── CSRF State ──────────────────────────────────────────────────

STATE_COOKIE = "_qvault_oauth_state"


def generate_state() -> str:
    return secrets.token_urlsafe(32)


# ── JWT Verification ────────────────────────────────────────────


def _load_public_key() -> str:
    global _pk_cache
    p = Path(settings.auth_public_key_path)
    try:
        mtime = os.path.getmtime(p)
    except OSError:
        if _pk_cache[1]:
            return _pk_cache[1]
        raise
    if mtime != _pk_cache[0]:
        _pk_cache = (mtime, p.read_text())
    return _pk_cache[1]


def _decode_jwt(token: str) -> dict | None:
    """Decode and verify a JWT from the OIDC provider. Returns payload or None."""
    try:
        return jwt.decode(
            token,
            _load_public_key(),
            algorithms=[_ALGORITHM],
            options={"verify_aud": False},
            leeway=5,
        )
    except jwt.ExpiredSignatureError:
        logger.warning("JWT expired")
        return None
    except jwt.InvalidTokenError as e:
        logger.warning("JWT validation failed: {}", e)
        return None


# ── User Sync ───────────────────────────────────────────────────


async def _sync_user(db: AsyncSession, username: str, org_id: str | None) -> FAUser:
    """Find or auto-provision a user, syncing IdP fields."""
    result = await db.execute(select(FAUser).where(FAUser.employee_name == username))
    user = result.scalar_one_or_none()

    if user is None:
        user = FAUser(employee_name=username, org_id=org_id)
        db.add(user)
        await db.commit()
        await db.refresh(user)
        logger.info("Auto-provisioned user '{}' via JWT", username)
    elif org_id and user.org_id != org_id:
        user.org_id = org_id
        await db.commit()

    return user


# ── Scope Checking ──────────────────────────────────────────────

_DEV_SCOPES = ["read", "write", "admin"]


def check_scopes(required: list[str], granted: list[str]) -> None:
    """Raise 403 if granted scopes don't satisfy required scopes.

    The "admin" scope implicitly satisfies any requirement.
    """
    if required and "admin" not in granted:
        for scope in required:
            if scope not in granted:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Insufficient scope: '{scope}' required.",
                )


# ── FastAPI Security Dependency ─────────────────────────────────


async def get_web_user(
    security_scopes: SecurityScopes,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> FAUser:
    """FastAPI Security dependency: validate session → JWT, enforce scopes, return user.

    Usage in routes::

        user: FAUser = Security(get_web_user, scopes=["read"])
        user: FAUser = Security(get_web_user, scopes=["write"])
        user: FAUser = Security(get_web_user, scopes=["admin"])
    """
    # Dev mode bypass
    if settings.dev_skip_auth:
        if os.environ.get("ENVIRONMENT") == "production":
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Security configuration error: DEV_SKIP_AUTH in production",
            )
        user = await _sync_user(db, "dev", "dev")
        user.jwt_scopes = _DEV_SCOPES  # type: ignore[attr-defined]
        return user

    # Read session cookie
    cookie = request.cookies.get(SESSION_COOKIE)
    if not cookie:
        raise HTTPException(
            status_code=status.HTTP_307_TEMPORARY_REDIRECT,
            headers={"Location": "/auth/login?next=" + str(request.url.path)},
        )

    token = read_session_cookie(cookie)
    if token is None:
        raise HTTPException(
            status_code=status.HTTP_307_TEMPORARY_REDIRECT,
            headers={"Location": "/auth/login?next=" + str(request.url.path)},
        )

    payload = _decode_jwt(token)
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_307_TEMPORARY_REDIRECT,
            headers={"Location": "/auth/login?next=" + str(request.url.path)},
        )

    username: str = payload.get("sub", "")
    token_scopes: list[str] = payload.get("scopes", [])
    org_id: str = payload.get("org_id", "")

    check_scopes(security_scopes.scopes, token_scopes)

    user = await _sync_user(db, username, org_id)
    user.jwt_scopes = token_scopes  # type: ignore[attr-defined]
    return user
