"""QVault — FastAPI application entry point."""

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Security
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from httpx import Timeout
from loguru import logger
from openai import AsyncOpenAI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.types import ASGIApp, Receive, Scope, Send

from app.core.auth import get_web_user
from app.core.config import settings
from app.core.logging_config import setup_logging
from app.models.fa_case import FAUser
from app.routers import cases, pages, triage, upload
from app.services.vlm_extractor import reset_semaphore

setup_logging()

BASE_DIR = Path(__file__).resolve().parent.parent


def _cleanup_stale_files() -> None:
    """Remove extraction_results.json files older than 7 days (abandoned reviews)."""
    import time

    images_dir = settings.images_path
    if not images_dir.exists():
        return
    cutoff = time.time() - 7 * 86400
    count = 0
    for results_file in images_dir.glob("*/extraction_results.json"):
        try:
            if results_file.stat().st_mtime < cutoff:
                results_file.unlink()
                count += 1
        except OSError:
            pass
    if count:
        logger.info("Cleaned up {} stale extraction result files", count)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- Startup ---
    # Database
    engine = create_async_engine(
        settings.database_url,
        echo=False,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_recycle=settings.db_pool_recycle,
        pool_pre_ping=True,
    )
    app.state.db_engine = engine
    app.state.db_session = async_sessionmaker(engine, expire_on_commit=False)
    logger.info("Database engine created")

    # VLM / Embedding client (shared AsyncOpenAI with connection pool)
    vlm_client = AsyncOpenAI(
        base_url=settings.vlm_base_url,
        api_key=settings.vlm_api_key,
        timeout=Timeout(settings.vlm_timeout, connect=10.0),
        max_retries=0,  # Retries handled at application level
    )
    app.state.vlm_client = vlm_client
    logger.info("VLM client created (timeout={}s)", settings.vlm_timeout)

    # VLM concurrency semaphore (bound to current event loop)
    reset_semaphore()

    # Background task tracking
    app.state.background_tasks: set[asyncio.Task] = set()

    # Clean up stale files from abandoned reviews
    _cleanup_stale_files()

    yield

    # --- Shutdown ---
    # Wait for in-flight background tasks before closing shared resources
    if app.state.background_tasks:
        logger.info(
            "Waiting for {} background tasks to complete...",
            len(app.state.background_tasks),
        )
        await asyncio.gather(*app.state.background_tasks, return_exceptions=True)

    await vlm_client.close()
    logger.info("VLM client closed")

    await engine.dispose()
    logger.info("Database engine disposed")


class SecurityHeadersMiddleware:
    """Pure ASGI middleware — injects security headers without buffering the body.

    Unlike BaseHTTPMiddleware, this does not wrap responses in StreamingResponse,
    so SSE (text/event-stream) connections stream correctly.
    """

    # Content-Security-Policy: restrict script/style sources to known CDNs.
    # 'unsafe-inline' is needed for existing inline <script> blocks and onclick
    # handlers. Refactoring to nonce-based CSP is a future improvement.
    CSP = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://cdn.tailwindcss.com https://unpkg.com; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "object-src 'none'; "
        "base-uri 'self'; "
        "frame-ancestors 'none'; "
        "form-action 'self'"
    )

    HEADERS = [
        (b"x-content-type-options", b"nosniff"),
        (b"x-frame-options", b"DENY"),
        (b"referrer-policy", b"strict-origin-when-cross-origin"),
        (b"content-security-policy", CSP.encode()),
    ]

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.extend(self.HEADERS)
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_headers)


app = FastAPI(
    title="QVault",
    description="失效分析周報結構化提取系統",
    version="0.1.0",
    lifespan=lifespan,
)
app.add_middleware(SecurityHeadersMiddleware)

# Static files (CSS, JS)
static_dir = BASE_DIR / "app" / "static"
static_dir.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=static_dir), name="static")

# Serve uploaded images (auth-gated, path from UPLOAD_DIR config)
uploads_dir = settings.upload_path


@app.get("/uploads/{file_path:path}")
async def serve_upload(
    file_path: str,
    user: FAUser = Security(get_web_user, scopes=["read"]),
):
    full_path = (uploads_dir / file_path).resolve()
    if not full_path.is_relative_to(uploads_dir.resolve()):
        raise HTTPException(status_code=404, detail="Not found")
    if not full_path.is_file():
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(full_path)


# Routers
app.include_router(upload.router)
app.include_router(triage.router)
app.include_router(cases.router)
app.include_router(pages.router)


@app.get("/health")
async def health():
    return {"status": "ok"}
