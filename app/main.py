"""FA Insight Harvester — FastAPI application entry point."""

import logging

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.routers import auth, cases, pages, upload

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

app = FastAPI(
    title="FA Insight Harvester",
    description="失效分析周報結構化提取系統",
    version="0.1.0",
)

# Static files (CSS, JS)
app.mount("/static", StaticFiles(directory="app/static"), name="static")

# Serve uploaded images
app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")

# Routers
app.include_router(auth.router)
app.include_router(upload.router)
app.include_router(cases.router)
app.include_router(pages.router)


@app.get("/health")
async def health():
    return {"status": "ok"}
