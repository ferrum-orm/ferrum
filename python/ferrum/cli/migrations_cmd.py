"""``ferrum migrations`` subcommands: dry-run and apply.

Wraps ``ferrum.migrations.orchestrator`` with CLI-friendly output and token
handling. Tokens are read from ``--token`` or ``FERRUM_MIGRATION_TOKEN``
environment variable — never from argv positional arguments (MIG-7).

The ``apply`` subcommand reads a plan JSON file produced by the Rust core,
then delegates to ``ferrum.migrations.apply()``.  When ``--dry-run`` is
passed (or when no plan file is given), only the plan is printed without
touching the database.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import typer


def migrations_dry_run(
    *,
    plan_file: Path | None = None,
    environment: str = "development",
) -> None:
    """Dry-run a migration plan JSON file."""
    token = os.environ.get("FERRUM_MIGRATION_TOKEN")
    asyncio.run(
        _apply(
            plan_file=str(plan_file) if plan_file is not None else None,
            confirm=False,
            do_dry_run=True,
            environment=environment,
            token=token,
        )
    )


def migrations_apply(
    *,
    plan_file: Path | None = None,
    token: str | None = None,
    confirm: bool = False,
    dry_run: bool = False,
    environment: str = "development",
) -> None:
    """Apply a migration plan JSON file."""
    resolved_token = token or os.environ.get("FERRUM_MIGRATION_TOKEN")
    derived_confirm = confirm or (resolved_token is not None)
    asyncio.run(
        _apply(
            plan_file=str(plan_file) if plan_file is not None else None,
            confirm=derived_confirm,
            do_dry_run=dry_run,
            environment=environment,
            token=resolved_token,
        )
    )


async def _apply(
    *,
    plan_file: str | None,
    confirm: bool,
    do_dry_run: bool,
    environment: str,
    token: str | None = None,
) -> None:
    """Load a plan JSON file and apply (or dry-run) it.

    Reads ``FERRUM_DATABASE_URL`` from the environment for the connection DSN.
    Exits with code 1 on configuration or migration errors.
    """
    if plan_file is None:
        print(
            "Error: plan_file is required for 'ferrum migrations apply'.\n"
            "Usage: ferrum migrations apply <plan_file> [--confirm] [--dry-run]",
        )
        raise typer.Exit(code=1)

    dsn = os.environ.get("FERRUM_DATABASE_URL")
    if dsn is None and not do_dry_run:
        # DSN is only required for a live apply; dry-run works without a connection.
        print(
            "Error: FERRUM_DATABASE_URL environment variable is not set. "
            "Set it before running 'ferrum migrations apply'. [FERR-C001]",
        )
        raise typer.Exit(code=1)

    try:
        with open(plan_file, encoding="utf-8") as fh:
            plan_json = fh.read()
    except OSError as exc:
        print(f"Error: could not read plan file '{plan_file}': {exc}")
        raise typer.Exit(code=1) from None

    from ferrum.errors import FerrumMigrationError
    from ferrum.migrations import apply

    if do_dry_run:
        # Dry-run does not need a DB connection; pass a sentinel conn object.
        # apply() with dry_run=True never calls conn._require_driver().
        from ferrum.connection import Connection

        conn = Connection.__new__(Connection)
        conn._pool = None  # type: ignore[attr-defined]

        result = await apply(
            conn, plan_json, dry_run=True, confirm=confirm, env=environment, token=token
        )
        status = "dry-run"
    else:
        from ferrum.connection import connect

        try:
            async with connect(dsn) as conn:
                result = await apply(
                    conn, plan_json, dry_run=False, confirm=confirm, env=environment, token=token
                )
        except FerrumMigrationError as exc:
            print(f"Error: {exc}")
            raise typer.Exit(code=1) from None
        status = "applied"

    print(f"[ferrum migrate] {status} {result.ops_count} ops")
