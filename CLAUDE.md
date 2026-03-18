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
  → Stage 1: VLM classification (case vs non-case, concurrent + retry)
  → Triage UI (user confirms/overrides classifications)
  → Stage 2: VLM field extraction (10 structured fields, concurrent + retry)
  → data cleaning → Review UI (user edits extracted fields)
  → confirm & save to DB → generate text + image embeddings
  → generate weekly summary via LLM
```

Source PPTX is deleted after conversion. On processing error before DB commit, the entire output directory is cleaned up. Stale `extraction_results.json` files (from abandoned reviews) are purged on startup (7-day TTL).

### Resource lifecycle (lifespan)

All shared connections are created in `app/main.py:lifespan` and stored on `app.state`:
- `app.state.db_engine` / `app.state.db_session` — SQLAlchemy async engine + session factory (pool_size, max_overflow, pool_recycle configurable via env)
- `app.state.vlm_client` — single `AsyncOpenAI` client for both VLM extraction and embeddings
- `app.state.background_tasks` — tracked `set[asyncio.Task]` for graceful shutdown

On startup, lifespan also: resets the VLM concurrency semaphore (binds to current event loop), and cleans up stale extraction result files.

Services receive the client as a parameter (not via module-level globals). DB sessions are obtained via `get_db(request)` dependency which reads from `request.app.state.db_session`.

### Key service roles

- **`vlm_extractor.py`** — Two-stage VLM pipeline (classify + extract) with `response_format` (structured output via Pydantic schemas). Handles concurrency (`vlm_max_concurrency` semaphore, reset on startup) and retry for transient errors.
- **`embedding.py`** — Qwen3-VL Chat Embeddings API via `client.post("/embeddings", ...)` with messages format (not standard `client.embeddings.create`).
- **`pptx_parser.py`** — PPTX → PDF → PNG conversion (requires LibreOffice + poppler-utils on system). Also does keyword-based pre-filtering.
- **`data_cleaner.py`** — Post-extraction field normalization.
- **`weekly_summary.py`** — Generates weekly summary from all cases in a period via LLM. Uses `load_only()` to avoid loading embedding vectors.
- **`audit.py`** — Records user actions (upload, confirm, edit, delete) to `audit_logs` table.

### Background tasks

Use `track_task(task, app.state.background_tasks, "name")` from `app/core/tasks.py` for all background tasks. This replaces the raw `add_done_callback(discard)` pattern — it tracks the task for graceful shutdown AND logs exceptions that would otherwise be silently swallowed.

### Admin endpoints

- `POST /api/cases/regenerate-embeddings` — Regenerate missing embeddings (requires `admin` scope)
- `POST /api/admin/archive-vlm-responses?days=90` — Null out `raw_vlm_response` on old cases to reclaim DB space (requires `admin` scope)

### Auth

Authentication is handled by **oauth2-proxy** (Docker container) + **Nginx** `auth_request`. The app never touches the OAuth flow directly — it only receives a verified JWT via the `Authorization: Bearer` header that Nginx injects from oauth2-proxy's `X-Auth-Request-Access-Token` response header. The app verifies the JWT with Auth Center's RS256 public key (`keys/public.pem`). Set `DEV_SKIP_AUTH=true` to bypass for local dev (returns a hardcoded dev user). Auth check is done per-route via `get_current_user_payload(request)`.

### Logging

Loguru is the sole logging backend. Configured once in `app/core/logging_config.py:setup_logging()`. Stdlib logging is intercepted via `_InterceptHandler` so third-party libs (uvicorn, sqlalchemy) also route through loguru. Use `logger.info("msg {}", var)` style (not f-strings) for deferred formatting.

### Frontend

Jinja2 templates in `app/templates/` with TailwindCSS (CDN) + HTMX. NotebookLM-inspired dark sidebar UI. SSE for real-time upload progress (`sse-starlette`).

## Database

PostgreSQL-only — uses `pgvector` (Vector columns), `ARRAY(Text)`, and GIN indexes. Not compatible with SQLite. Embedding dimension is 1024.

## Environment

Single `.env` file (see `.env.example`) shared by App, Docker Compose, and systemd. Loaded by `pydantic-settings` in `app/core/config.py`. Set `DATA_DIR` once — `UPLOAD_DIR`, `LOG_DIR`, `AUTH_PUBLIC_KEY_PATH` are auto-derived. Set `PG_USER`/`PG_PASSWORD`/`PG_PORT`/`PG_DB` — `DATABASE_URL` is auto-derived. Individual vars can still be overridden explicitly.

## System dependencies

LibreOffice (`libreoffice --headless`) and poppler-utils (`pdftoppm`) must be installed for PPTX-to-image conversion.
