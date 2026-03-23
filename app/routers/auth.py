"""Auth routes — OIDC login, callback, logout."""

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from loguru import logger

from app.core.auth import (
    SESSION_COOKIE,
    STATE_COOKIE,
    _get_oidc_config,
    create_session_cookie,
    exchange_code,
    generate_state,
    get_authorization_url,
    read_session_cookie,
)
from app.core.config import settings

router = APIRouter(prefix="/auth", tags=["auth"])


@router.get("/login")
async def login(request: Request, next: str = "/"):
    """Redirect user to OIDC provider for authentication."""
    # If already logged in, skip
    cookie = request.cookies.get(SESSION_COOKIE)
    if cookie and read_session_cookie(cookie) is not None:
        return RedirectResponse(next, status_code=302)

    # Ensure OIDC config is loaded
    await _get_oidc_config()

    state = generate_state()
    url = get_authorization_url(state)

    response = RedirectResponse(url, status_code=302)
    # Store state + return path in a short-lived cookie for CSRF validation
    response.set_cookie(
        STATE_COOKIE,
        f"{state}|{next}",
        max_age=600,
        httponly=True,
        samesite="lax",
    )
    return response


@router.get("/callback")
async def callback(request: Request, code: str = "", state: str = ""):
    """Handle OIDC callback — exchange code for tokens, set session cookie."""
    # Validate state (CSRF protection)
    state_cookie = request.cookies.get(STATE_COOKIE)
    if not state_cookie or "|" not in state_cookie:
        logger.warning("Missing or malformed state cookie")
        return RedirectResponse("/auth/login", status_code=302)

    expected_state, return_path = state_cookie.split("|", 1)
    if state != expected_state:
        logger.warning("State mismatch: expected={}, got={}", expected_state, state)
        return RedirectResponse("/auth/login", status_code=302)

    if not code:
        logger.warning("Missing authorization code in callback")
        return RedirectResponse("/auth/login", status_code=302)

    # Exchange code for tokens
    try:
        token_data = await exchange_code(code)
    except Exception:
        logger.exception("Token exchange failed")
        return RedirectResponse("/auth/login", status_code=302)

    access_token = token_data.get("access_token", "")
    if not access_token:
        logger.warning("No access_token in token response")
        return RedirectResponse("/auth/login", status_code=302)

    # Set session cookie with signed access token
    response = RedirectResponse(return_path or "/", status_code=302)
    response.set_cookie(
        SESSION_COOKIE,
        create_session_cookie(access_token),
        max_age=86400,  # 24 hours
        httponly=True,
        samesite="lax",
        secure=settings.oauth2_redirect_url.startswith("https"),
    )
    # Clear state cookie
    response.delete_cookie(STATE_COOKIE)
    return response


@router.get("/logout")
async def logout(request: Request):
    """Clear session cookie and optionally redirect to OIDC end_session."""
    response = RedirectResponse("/", status_code=302)
    response.delete_cookie(SESSION_COOKIE)
    return response
