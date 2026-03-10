# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install dependencies
uv sync

# Run dev server (skips OAuth, DEBUG logging)
DEV_SKIP_AUTH=true uv run fastapi run app/main.py

# Run with hot-reload
uv run uvicorn app.main:app --reload

# Database migrations
uv run alembic upgrade head       # apply all
uv run alembic revision --autogenerate -m "description"  # create new

# Lint
uv run ruff check .
uv run ruff format .

# Tests
uv run pytest
uv run pytest tests/test_foo.py::test_bar  # single test
```

## Architecture

**FastAPI app with Jinja2 SSR frontend.** Extracts structured FA case data from PowerPoint weekly reports using a Vision Language Model, stores in PostgreSQL with pgvector embeddings.

### Processing pipeline

```
Upload .pptx → LibreOffice → PDF → pdftoppm → per-slide PNGs
  → keyword pre-filter (skip non-case slides)
  → VLM structured extraction (concurrent, with retry)
  → data cleaning → user review UI → confirm & save to DB
  → generate text + image embeddings
```

### Resource lifecycle (lifespan)

All shared connections are created in `app/main.py:lifespan` and stored on `app.state`:
- `app.state.db_engine` / `app.state.db_session` — SQLAlchemy async engine + session factory
- `app.state.vlm_client` — single `AsyncOpenAI` client for both VLM extraction and embeddings

Services receive the client as a parameter (not via module-level globals). DB sessions are obtained via `get_db(request)` dependency which reads from `request.app.state.db_session`.

### Key service roles

- **`vlm_extractor.py`** — Sends slide images to VLM with `response_format` (structured output via Pydantic schema `VLMSlideResult`). Handles concurrency (`vlm_max_concurrency` semaphore) and retry.
- **`embedding.py`** — Qwen3-VL Chat Embeddings API via `client.post("/embeddings", ...)` with messages format (not standard `client.embeddings.create`).
- **`pptx_parser.py`** — PPTX → PDF → PNG conversion (requires LibreOffice + poppler-utils on system). Also does keyword-based pre-filtering.
- **`data_cleaner.py`** — Post-extraction field normalization.

### Auth

OAuth 2.0 Authorization Code flow with JWT RS256. Set `DEV_SKIP_AUTH=true` to bypass for local dev (returns a hardcoded dev user). Auth check is done per-route via `get_current_user_payload(request)`.

### Logging

Loguru is the sole logging backend. Configured once in `app/core/logging_config.py:setup_logging()`. Stdlib logging is intercepted via `_InterceptHandler` so third-party libs (uvicorn, sqlalchemy) also route through loguru. Use `logger.info("msg {}", var)` style (not f-strings) for deferred formatting.

### Frontend

Jinja2 templates in `app/templates/` with TailwindCSS (CDN) + HTMX. NotebookLM-inspired dark sidebar UI. SSE for real-time upload progress (`sse-starlette`).

## Database

PostgreSQL-only — uses `pgvector` (Vector columns), `ARRAY(Text)`, and GIN indexes. Not compatible with SQLite. Embedding dimension is 1024.

## Environment

All config via env vars or `.env` file (see `.env.example`). Loaded by `pydantic-settings` in `app/core/config.py`. Key vars: `DATABASE_URL`, `VLM_BASE_URL`, `VLM_MODEL`, `VLM_EMBEDDING_MODEL`, `DEV_SKIP_AUTH`.

## System dependencies

LibreOffice (`libreoffice --headless`) and poppler-utils (`pdftoppm`) must be installed for PPTX-to-image conversion.
