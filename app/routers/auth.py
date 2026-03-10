"""Auth routes: login page, OAuth redirect, callback."""

from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import exchange_code_for_token, get_login_url, get_or_create_user, verify_token
from app.core.config import settings
from app.models.database import get_db

router = APIRouter(prefix="/auth", tags=["auth"])
templates = Jinja2Templates(directory=Path(__file__).resolve().parent.parent / "templates")


@router.get("/login", response_class=HTMLResponse)
async def login(request: Request):
    """Render login landing page."""
    if settings.dev_skip_auth:
        return RedirectResponse(url="/")
    return templates.TemplateResponse("login.html", {"request": request})


@router.get("/oauth-redirect")
async def oauth_redirect(request: Request):
    """Redirect to external Auth Center for OAuth login."""
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
