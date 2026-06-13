# Ferrum + FastAPI Quickstart

A minimal runnable example demonstrating Ferrum with a FastAPI application.

## Prerequisites

```bash
pip install "ferrum[fastapi]" fastapi uvicorn
maturin develop  # or: pip install ferrum (once wheels are published)
```

## Run

```bash
cp .env.example .env
docker compose up -d
uvicorn app:app --reload
```

## What this example shows

- Model definition with Pydantic v2.
- `ferrum_lifespan` for connection pool lifecycle.
- Async query patterns (`filter`, `get`, `all`).
- Migration dry-run and apply via the CLI.
