"""Auth routes: login page, logout redirect.

OAuth flow is handled entirely by oauth2-proxy + Nginx.
The app only provides a login landing page and redirects.
"""

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.core.config import settings

router = APIRouter(prefix="/auth", tags=["auth"])
templates = Jinja2Templates(
    directory=Path(__file__).resolve().parent.parent / "templates"
)


@router.get("/login", response_class=HTMLResponse)
async def login(request: Request):
    """Render login landing page."""
    if settings.dev_skip_auth:
        return RedirectResponse(url="/")
    return templates.TemplateResponse("login.html", {"request": request})


@router.get("/logout")
async def logout():
    """Redirect to oauth2-proxy sign-out endpoint."""
    if settings.dev_skip_auth:
        return RedirectResponse(url="/auth/login", status_code=302)
    return RedirectResponse(url="/oauth2/sign_out", status_code=302)
