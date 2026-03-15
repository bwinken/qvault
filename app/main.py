"""QVault — FastAPI application entry point."""

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from loguru import logger
from openai import AsyncOpenAI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import settings
from app.core.logging_config import setup_logging
from app.routers import auth, cases, pages, triage, upload

setup_logging()

BASE_DIR = Path(__file__).resolve().parent.parent


@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- Startup ---
    # Database
    engine = create_async_engine(settings.database_url, echo=False)
    app.state.db_engine = engine
    app.state.db_session = async_sessionmaker(engine, expire_on_commit=False)
    logger.info("Database engine created")

    # VLM / Embedding client (shared AsyncOpenAI with connection pool)
    vlm_client = AsyncOpenAI(
        base_url=settings.vlm_base_url,
        api_key=settings.vlm_api_key,
    )
    app.state.vlm_client = vlm_client
    logger.info("VLM client created")

    yield

    # --- Shutdown ---
    await vlm_client.close()
    logger.info("VLM client closed")

    await engine.dispose()
    logger.info("Database engine disposed")


app = FastAPI(
    title="QVault",
    description="失效分析周報結構化提取系統",
    version="0.1.0",
    lifespan=lifespan,
)

# Static files (CSS, JS)
static_dir = BASE_DIR / "app" / "static"
static_dir.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=static_dir), name="static")

# Serve uploaded images
uploads_dir = BASE_DIR / "uploads"
uploads_dir.mkdir(parents=True, exist_ok=True)
app.mount("/uploads", StaticFiles(directory=uploads_dir), name="uploads")

# Routers
app.include_router(auth.router)
app.include_router(upload.router)
app.include_router(triage.router)
app.include_router(cases.router)
app.include_router(pages.router)


@app.get("/health")
async def health():
    return {"status": "ok"}
