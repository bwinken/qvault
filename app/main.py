"""FA Insight Harvester — FastAPI application entry point."""

import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.routers import auth, cases, pages, upload

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

BASE_DIR = Path(__file__).resolve().parent.parent

app = FastAPI(
    title="FA Insight Harvester",
    description="失效分析周報結構化提取系統",
    version="0.1.0",
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
app.include_router(cases.router)
app.include_router(pages.router)


@app.get("/health")
async def health():
    return {"status": "ok"}
