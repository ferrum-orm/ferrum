"""SQLite driver example — no Docker required.

Uses an on-disk ``sqlite:///`` DSN. Schema is applied programmatically from a
migration plan JSON (same shape as ``ferrum migrations apply``).

Run from ``examples/sqlite``::

    uv sync --extra dev --directory ../..
    mise run dev --directory ../..
    uv run python main.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import ferrum
from ferrum import Model, connect
from ferrum.migrations import apply


class Note(Model):
    id: int = 0
    body: str = ""


async def _ensure_schema(conn: ferrum.Connection) -> None:
    plan_path = Path(__file__).parent / "plans" / "001_create_note.json"
    plan_json = plan_path.read_text(encoding="utf-8")
    result = await apply(conn, plan_json, dry_run=False)
    if result.applied:
        print(f"schema ready ({result.ops_count} op(s) applied)")


async def main() -> None:
    db_file = Path(__file__).parent / "ferrum_example.db"
    # Relative file DSN (three slashes) — avoids Unix absolute-path parsing quirks.
    dsn = f"sqlite:///{db_file.name}"

    async with connect(dsn) as conn:
        print(f"connected dialect={conn.dialect} dsn={dsn}")
        await _ensure_schema(conn)

        note = await Note.objects.create(conn, body="Hello from Ferrum on SQLite")
        print(f"created id={note.id} body={note.body!r}")

        fetched = await Note.objects.filter(id=note.id).get(conn)
        print(f"get     id={fetched.id} body={fetched.body!r}")

        remaining = await Note.objects.count(conn)
        print(f"count   notes={remaining}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except ferrum.FerrumConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        print("Install SQLite support: uv add 'ferrum-orm[sqlite]'", file=sys.stderr)
        raise SystemExit(1) from exc
    except ferrum.FerrumError as exc:
        print(f"Ferrum error [{exc.code}]: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
