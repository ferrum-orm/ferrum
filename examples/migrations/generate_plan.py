"""Generate a migration plan JSON from Ferrum models.

Compares model metadata against the live PostgreSQL schema and writes a plan
file suitable for ``ferrum migrations apply``.

Usage (from examples/migrations):

    export FERRUM_DATABASE_URL=postgresql://ferrum:changeme@127.0.0.1:5432/ferrum_dev
    uv run python generate_plan.py --write plans/auto.json
    uv run python generate_plan.py --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from models import Note

from ferrum import connect
from ferrum.migrations import apply, compute_plan

MODELS = [Note]


async def _fetch_existing_tables(conn) -> dict[str, list[str]]:  # noqa: ANN001
    """Introspect public tables via information_schema."""
    pool = conn._require_pool()
    rows = await pool.fetch(
        """
        SELECT table_name, column_name
        FROM information_schema.columns
        WHERE table_schema = 'public'
        ORDER BY table_name, ordinal_position
        """
    )
    tables: dict[str, list[str]] = {}
    for row in rows:
        tables.setdefault(row["table_name"], []).append(row["column_name"])
    return tables


async def _run(*, write: Path | None, dry_run: bool) -> None:
    async with connect() as conn:
        existing = await _fetch_existing_tables(conn)
        plan = compute_plan(MODELS, existing)
        plan_json = json.dumps(plan, indent=2)

        if write is not None:
            write.parent.mkdir(parents=True, exist_ok=True)
            write.write_text(plan_json + "\n", encoding="utf-8")
            print(f"Wrote plan ({len(plan['ops'])} ops) to {write}")

        if dry_run or write is None:
            await apply(conn, plan_json, dry_run=True)

        if not dry_run and write is not None:
            print("Plan written. Apply with:")
            print(f"  ferrum migrations apply {write} --confirm")


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate a Ferrum migration plan")
    parser.add_argument(
        "--write",
        type=Path,
        metavar="PATH",
        help="Write plan JSON to this file",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the plan without writing a file",
    )
    args = parser.parse_args()

    if args.write is None and not args.dry_run:
        parser.error("Pass --write PATH and/or --dry-run")

    try:
        asyncio.run(_run(write=args.write, dry_run=args.dry_run))
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
