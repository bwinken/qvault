"""JWT authentication via oauth2-proxy.

oauth2-proxy handles the full OAuth 2.0 flow (login, callback, token exchange).
Nginx injects the access token as an Authorization header via auth_request.
This module only verifies the JWT and extracts user info.
"""

import os
from pathlib import Path

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer, SecurityScopes
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.database import get_db
from app.models.fa_case import FAUser

_ALGORITHM = "RS256"
_bearer = HTTPBearer(auto_error=False)

# Cache public key with mtime check so key rotation takes effect without restart.
_pk_cache: tuple[float, str] = (0.0, "")


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
    """Decode and verify a JWT from Auth Center. Returns payload or None."""
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


async def get_web_user(
    security_scopes: SecurityScopes,
    request: Request,
    db: AsyncSession = Depends(get_db),
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> FAUser:
    """FastAPI Security dependency: validate JWT, enforce scopes, return user.

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

    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing access token.",
        )

    payload = _decode_jwt(credentials.credentials)
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid access token.",
        )

    username: str = payload.get("sub", "")
    token_scopes: list[str] = payload.get("scopes", [])
    org_id: str = payload.get("org_id", "")

    check_scopes(security_scopes.scopes, token_scopes)

    user = await _sync_user(db, username, org_id)
    user.jwt_scopes = token_scopes  # type: ignore[attr-defined]
    return user
