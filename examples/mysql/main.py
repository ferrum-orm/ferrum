"""MySQL driver example — asyncmy backend.

MySQL thin parity does not yet map ``INTEGER PRIMARY KEY`` migration columns to
``AUTO_INCREMENT``; this demo supplies explicit ``id`` values on insert. For
production schemas, use ``ferrum makemigrations`` from models with appropriate
``db_default`` / field metadata.

Run from ``examples/mysql`` after MySQL is up::

    export FERRUM_DATABASE_URL=mysql://ferrum:changeme@127.0.0.1:3306/ferrum_dev
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
    async with connect() as conn:
        print(f"connected dialect={conn.dialect}")
        await _ensure_schema(conn)

        # Explicit PK until AUTO_INCREMENT is emitted for MySQL migration plans.
        note = await Note.objects.create(conn, id=1, body="Hello from Ferrum on MySQL")
        print(f"created id={note.id} body={note.body!r}")

        fetched = await Note.objects.filter(id=note.id).get(conn)
        print(f"get     id={fetched.id} body={fetched.body!r}")

        total = await Note.objects.count(conn)
        print(f"count   notes={total}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except ferrum.FerrumConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        print("Install MySQL support: uv add 'ferrum-orm[mysql]'", file=sys.stderr)
        raise SystemExit(1) from exc
    except ferrum.FerrumError as exc:
        print(f"Ferrum error [{exc.code}]: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
