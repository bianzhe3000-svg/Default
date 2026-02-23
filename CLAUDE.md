# CLAUDE.md — AI Assistant Guide for podcast-automation

This file provides context and conventions for AI assistants (Claude Code and similar tools) working in this repository.

---

## Project Overview

**podcast-automation** is a Python-based backend service that automates the end-to-end processing of podcasts:

1. Discover and validate RSS feeds (iTunes / Spotify APIs)
2. Transcribe audio episodes via OpenAI Whisper
3. Analyze Chinese-language content with GPT-4
4. Generate structured Markdown summaries via Jinja2 templates
5. Schedule and track all jobs with APScheduler + SQLite

The API surface is a **FastAPI** application running on `http://localhost:8000`.

---

## Repository Layout

```
podcast-automation/
├── src/                        # All application source code
│   ├── main.py                 # FastAPI app factory, lifespan hooks
│   ├── config.py               # Pydantic-Settings + YAML config loader
│   ├── database.py             # SQLAlchemy async engine & session factory
│   ├── api/
│   │   ├── routes.py           # All REST endpoint definitions
│   │   └── schemas.py          # Pydantic request / response schemas
│   ├── models/
│   │   ├── podcast.py          # Podcast & Episode ORM models
│   │   └── task.py             # TaskLog ORM model
│   ├── modules/
│   │   ├── rss_discovery.py    # iTunes/Spotify search + fuzzy dedup
│   │   ├── rss_validator.py    # Feed fetch, parse, incremental update
│   │   ├── audio_transcriber.py# Whisper API wrapper
│   │   ├── content_analyzer.py # GPT-4 semantic analysis (Chinese focus)
│   │   ├── markdown_generator.py # Jinja2 → Markdown document builder
│   │   └── opml_parser.py      # OPML import / export
│   ├── scheduler/
│   │   └── task_scheduler.py   # APScheduler cron orchestration
│   └── utils/
│       ├── logger.py           # Structured logging helper
│       ├── retry.py            # Exponential-backoff retry decorator
│       └── memory_monitor.py   # psutil-based memory guard
├── tests/                      # pytest test suite (mirrors src/ structure)
├── templates/
│   └── podcast_summary.md.j2  # Jinja2 template for Markdown output
├── config/
│   └── config.example.yaml    # Annotated reference configuration
├── summaries/                  # Runtime output: generated .md files
├── Dockerfile
├── docker-compose.yml
├── pyproject.toml              # Project metadata, pytest, ruff, mypy config
├── requirements.txt            # Production dependencies
└── .env.example               # Required environment variable template
```

---

## Tech Stack

| Layer | Technology |
|---|---|
| Web framework | FastAPI (async) |
| ORM | SQLAlchemy 2.x (async) + aiosqlite |
| Database | SQLite (WAL mode, foreign keys on) |
| Task scheduling | APScheduler |
| AI / Transcription | OpenAI Whisper + GPT-4 |
| Templates | Jinja2 |
| Config | Pydantic-Settings + PyYAML |
| Retry logic | tenacity |
| Fuzzy matching | fuzzywuzzy |
| HTTP client | httpx (async) |

---

## Environment Setup

### Prerequisites

- Python 3.10+
- `ffmpeg` (required by Whisper for audio processing)
- An OpenAI API key

### Local Development

```bash
# 1. Copy and populate environment variables
cp .env.example .env
# Edit .env: set OPENAI_API_KEY at minimum

# 2. Install dependencies
pip install -r requirements.txt

# 3. (Optional) copy and edit YAML config
cp config/config.example.yaml config/config.yaml

# 4. Run the service
python -m src.main
# API available at http://localhost:8000
# Swagger UI: http://localhost:8000/docs
```

### Docker

```bash
docker-compose up -d
# Health check endpoint: GET http://localhost:8000/api/health
```

---

## Running Tests

```bash
# Full test suite
pytest tests/ -v

# With HTML coverage report
pytest tests/ --cov=src --cov-report=html

# Single module
pytest tests/test_rss_validator.py -v
```

**Test conventions:**
- All tests live under `tests/` mirroring `src/` module names.
- Shared fixtures are in `tests/conftest.py` (in-memory SQLite, mock OpenAI client, sample RSS/OPML data).
- Tests use `pytest-asyncio` with `asyncio_mode = "auto"` (set in `pyproject.toml`).
- Never rely on a real network or real OpenAI API in unit tests — use the mock fixtures.

---

## Linting & Type Checking

```bash
# Lint
ruff check src/ tests/

# Auto-fix safe issues
ruff check --fix src/ tests/

# Type checking
mypy src/
```

**Rules (from pyproject.toml):**
- Line length: **100 characters**
- Target Python: **3.10**
- MyPy: strict return types, warn on missing imports

---

## Key Conventions

### Async-first

All database access and HTTP calls must be `async`. Use `async with get_db() as session:` for database sessions (never create raw sessions outside that context manager).

### Configuration

- Environment variables are defined in `.env` and loaded by `src/config.py` via Pydantic-Settings.
- YAML config (`config/config.yaml`) extends env vars with structured lists (podcast feeds, etc.).
- Never hard-code secrets or URLs — always read from `settings` (the `Settings` singleton).

### Database Models

- Located in `src/models/`.
- Use SQLAlchemy 2.x declarative style with `Mapped` / `mapped_column`.
- `ProcessingStatus` enum (on `Episode`): `pending → processing → completed | failed`.
- Always run migrations via Alembic when changing models (the app auto-creates tables on startup for development, but production changes require migrations).

### API Layer

- All route handlers live in `src/api/routes.py`.
- Request/response shapes are Pydantic models in `src/api/schemas.py`.
- Return HTTP 404 for missing resources, 422 for validation errors (FastAPI default), 500 only for unrecoverable server errors.
- The scheduler state (`is_running`, last run timestamps) is exposed via `GET /api/scheduler/status`.

### Error Handling & Retries

- Use the `@retry` decorator from `src/utils/retry.py` for any external API call (OpenAI, iTunes, Spotify, feed fetches).
- Default: 3 attempts, exponential backoff.
- Log every retry attempt at WARNING level; log final failure at ERROR level.

### Logging

- Use `get_logger(__name__)` from `src/utils/logger.py` — never use `print()`.
- Log levels: DEBUG for internal state, INFO for significant events, WARNING for recoverable issues, ERROR for failures.

### Memory Guard

- `src/utils/memory_monitor.py` enforces `MEMORY_LIMIT_MB` (default 1024 MB).
- Long-running batch jobs should call the monitor periodically to avoid OOM.

### Markdown Output

- Templates live in `templates/`. The only current template is `podcast_summary.md.j2`.
- Generated files are written to `summaries/` with filenames derived from episode slugs.
- Do not alter the Jinja2 template without also updating `src/modules/markdown_generator.py` and relevant tests.

---

## API Reference (Summary)

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/health` | Health check |
| GET | `/api/podcasts` | List all podcasts |
| POST | `/api/podcasts` | Add podcast by RSS URL |
| GET | `/api/podcasts/{id}` | Get podcast detail |
| DELETE | `/api/podcasts/{id}` | Remove podcast |
| GET | `/api/podcasts/{id}/episodes` | List episodes |
| POST | `/api/podcasts/{id}/refresh` | Force RSS refresh |
| GET | `/api/episodes/{id}` | Get episode detail |
| POST | `/api/episodes/{id}/transcribe` | Trigger transcription |
| POST | `/api/episodes/{id}/analyze` | Trigger content analysis |
| GET | `/api/documents` | List generated markdown docs |
| GET | `/api/documents/{id}` | Get document content |
| POST | `/api/search` | Search podcasts via iTunes/Spotify |
| POST | `/api/import/opml` | Import OPML feed list |
| GET | `/api/export/opml` | Export OPML feed list |
| POST | `/api/scheduler/start` | Start scheduled jobs |
| POST | `/api/scheduler/stop` | Stop scheduled jobs |
| GET | `/api/scheduler/status` | Scheduler state |

Full interactive docs available at `http://localhost:8000/docs`.

---

## Common Development Tasks

### Add a new API endpoint

1. Define the Pydantic schema in `src/api/schemas.py`.
2. Add the route handler in `src/api/routes.py` using the existing router.
3. Add a corresponding test in `tests/test_integration.py` using `httpx.AsyncClient`.

### Add a new module

1. Create `src/modules/your_module.py`.
2. Add `get_logger(__name__)` at the top.
3. Inject it into `src/main.py` lifespan and pass it to the router via `app.state`.
4. Write tests in `tests/test_your_module.py` with mocked external dependencies.

### Change the Markdown template

1. Edit `templates/podcast_summary.md.j2`.
2. Update `src/modules/markdown_generator.py` if new variables are needed.
3. Update `tests/test_markdown_generator.py` fixture data to match.

### Modify database schema

1. Edit the relevant model in `src/models/`.
2. Generate an Alembic migration: `alembic revision --autogenerate -m "description"`.
3. Review the generated migration file before applying.
4. Apply: `alembic upgrade head`.

---

## Important Files to Read First

When starting work in this repo, read these files in order:

1. `src/config.py` — understand all configurable settings
2. `src/main.py` — understand app startup, component wiring
3. `src/api/routes.py` — understand the full API surface
4. `src/models/podcast.py` — understand core data model
5. `tests/conftest.py` — understand test fixtures before writing tests

---

## What to Avoid

- **Do not** use `print()` — use the logger.
- **Do not** create synchronous database sessions — always use async.
- **Do not** call external APIs (OpenAI, iTunes, Spotify) in tests without mocking.
- **Do not** hard-code the SQLite path or API keys — read from `settings`.
- **Do not** write to `summaries/` directly — use `MarkdownGenerator`.
- **Do not** modify `requirements.txt` manually — add dependencies via pip and regenerate, or update `pyproject.toml`.
- **Do not** skip the retry decorator on any outbound HTTP or OpenAI call.
