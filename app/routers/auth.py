"""Auth routes: login redirect, OAuth callback."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import exchange_code_for_token, get_login_url, get_or_create_user, verify_token
from app.models.database import get_db

router = APIRouter(prefix="/auth", tags=["auth"])


@router.get("/login")
async def login(request: Request):
    """Redirect to Auth Center login page."""
    return RedirectResponse(url=get_login_url())


@router.get("/callback")
async def callback(
    code: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """OAuth callback: exchange code for JWT, set cookie, redirect to home."""
    token = await exchange_code_for_token(code)
    payload = verify_token(token)
    await get_or_create_user(db, payload)

    response = RedirectResponse(url="/", status_code=302)
    response.set_cookie(
        key="access_token",
        value=token,
        httponly=True,
        samesite="lax",
    )
    return response


@router.get("/logout")
async def logout():
    """Clear auth cookie and redirect to login."""
    response = RedirectResponse(url="/auth/login", status_code=302)
    response.delete_cookie("access_token")
    return response
