"""Simple Ferrum demo — async CRUD without a web framework.

Run from examples/simple after PostgreSQL is up and migrations are applied:

    export FERRUM_DATABASE_URL=postgresql://ferrum:changeme@127.0.0.1:5432/ferrum_dev
    uv run python main.py
"""

from __future__ import annotations

import asyncio
import sys

import ferrum
from ferrum import Model, clear_hooks, connect, register_hook


class Note(Model):
    id: int = 0
    body: str = ""


def _log_hook(payload: dict[str, object]) -> None:
    event = payload.get("event", "?")
    model = payload.get("model", "?")
    operation = payload.get("operation", "?")
    status = payload.get("status", "?")
    print(f"[hook] {event} model={model} op={operation} status={status}")


async def main() -> None:
    register_hook("*", _log_hook)
    try:
        async with connect() as conn:
            note = await Note.objects.create(conn, body="Hello from Ferrum")
            print(f"created id={note.id} body={note.body!r}")

            fetched = await Note.objects.filter(id=note.id).get(conn)
            print(f"get     id={fetched.id} body={fetched.body!r}")

            updated = await Note.objects.filter(id=note.id).update(conn, body="Updated")
            print(f"update  rows={updated}")

            total = await Note.objects.filter(id=note.id).count(conn)
            print(f"count   matching={total}")

            rows = await Note.objects.filter(id=note.id).all(conn)
            print(f"all     {[n.body for n in rows]}")

            deleted = await Note.objects.filter(id=note.id).delete(conn)
            print(f"delete  rows={deleted}")

            remaining = await Note.objects.count(conn)
            print(f"remaining notes={remaining}")
    except ferrum.FerrumConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        print("Build the extension with: mise run dev", file=sys.stderr)
        raise SystemExit(1) from exc
    except ferrum.FerrumError as exc:
        print(f"Ferrum error [{exc.code}]: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    finally:
        clear_hooks()


if __name__ == "__main__":
    asyncio.run(main())
