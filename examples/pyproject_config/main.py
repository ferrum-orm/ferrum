"""Pyproject.toml configuration example.

This directory has **no** ``ferrum.toml``. Ferrum reads the ``[ferrum]`` table from
``pyproject.toml`` and resolves the database URL from ``DATABASE_URL`` (not
``FERRUM_DATABASE_URL``) via ``database_url_env``.

Run from this directory::

    cp .env.example .env
    docker compose up -d
    export DATABASE_URL=postgresql://ferrum:changeme@127.0.0.1:5432/ferrum_dev
    ferrum migrations apply plans/001_create_note.json --confirm
    uv run python main.py
"""

from __future__ import annotations

import asyncio
import sys

from models import Note

import ferrum
from ferrum import connect


async def main() -> None:
    async with connect() as conn:
        print(f"connected dialect={conn.dialect}")

        note = await Note.objects.create(conn, body="Hello via DATABASE_URL + pyproject.toml")
        print(f"created id={note.id} body={note.body!r}")

        rows = await Note.objects.filter(id=note.id).all(conn)
        print(f"all     {[n.body for n in rows]}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except ferrum.FerrumConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        print(
            "Set DATABASE_URL (this example uses database_url_env in pyproject.toml).",
            file=sys.stderr,
        )
        raise SystemExit(1) from exc
    except ferrum.FerrumError as exc:
        print(f"Ferrum error [{exc.code}]: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
