"""OAuth 2.0 authentication with Auth Center (Authorization Code Flow)."""

from pathlib import Path

import httpx
import jwt
from fastapi import Cookie, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.fa_case import FAUser

_public_key: str | None = None


def _get_public_key() -> str:
    global _public_key
    if _public_key is None:
        key_path = Path(settings.oauth_public_key_path)
        if not key_path.exists():
            raise RuntimeError(f"OAuth public key not found: {key_path}")
        _public_key = key_path.read_text()
    return _public_key


def get_login_url(redirect_uri: str | None = None) -> str:
    """Build Auth Center login URL."""
    uri = redirect_uri or settings.oauth_redirect_uri
    return (
        f"{settings.oauth_auth_url}"
        f"?app_id={settings.oauth_client_id}"
        f"&redirect_uri={uri}"
    )


async def exchange_code_for_token(code: str) -> str:
    """Exchange authorization code for JWT token."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            settings.oauth_token_url,
            json={
                "code": code,
                "client_secret": settings.oauth_client_secret,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        return data["token"]


def verify_token(token: str) -> dict:
    """Verify JWT token using Auth Center's public key."""
    try:
        payload = jwt.decode(
            token,
            _get_public_key(),
            algorithms=["RS256"],
            audience=settings.oauth_client_id,
        )
        return payload
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token expired",
        )
    except jwt.InvalidTokenError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid token: {e}",
        )


def get_current_user_payload(request: Request) -> dict:
    """Extract and verify user from JWT cookie."""
    token = request.cookies.get("access_token")
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )
    return verify_token(token)


async def get_or_create_user(db: AsyncSession, payload: dict) -> FAUser:
    """Get or create FAUser from JWT payload."""
    employee_name = payload["sub"]
    org_id = payload.get("org_id")

    result = await db.execute(
        select(FAUser).where(FAUser.employee_name == employee_name)
    )
    user = result.scalar_one_or_none()

    if user is None:
        user = FAUser(employee_name=employee_name, org_id=org_id)
        db.add(user)
        await db.commit()
        await db.refresh(user)
    elif user.org_id != org_id:
        user.org_id = org_id
        await db.commit()

    return user
