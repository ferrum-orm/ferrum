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

import argparse
import asyncio
import os
import sys


def run_migrations(args: argparse.Namespace) -> None:
    """Dispatch to the appropriate migration subcommand."""
    cmd = getattr(args, "mig_command", None)
    token = getattr(args, "token", None) or os.environ.get("FERRUM_MIGRATION_TOKEN")
    if cmd == "dry-run":
        asyncio.run(
            _apply(
                plan_file=getattr(args, "plan_file", None),
                confirm=False,
                do_dry_run=True,
                environment=getattr(args, "environment", "development"),
                token=token,
            )
        )
    elif cmd == "apply":
        confirm = getattr(args, "confirm", False) or (token is not None)
        do_dry_run = getattr(args, "do_dry_run", False)
        plan_file = getattr(args, "plan_file", None)
        asyncio.run(
            _apply(
                plan_file=plan_file,
                confirm=confirm,
                do_dry_run=do_dry_run,
                environment=args.environment,
                token=token,
            )
        )
    else:
        print("Usage: ferrum migrations <dry-run | apply>")
        sys.exit(1)


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
            file=sys.stderr,
        )
        sys.exit(1)

    dsn = os.environ.get("FERRUM_DATABASE_URL")
    if dsn is None and not do_dry_run:
        # DSN is only required for a live apply; dry-run works without a connection.
        print(
            "Error: FERRUM_DATABASE_URL environment variable is not set. "
            "Set it before running 'ferrum migrations apply'. [FERR-C001]",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        with open(plan_file) as fh:
            plan_json = fh.read()
    except OSError as exc:
        print(f"Error: could not read plan file '{plan_file}': {exc}", file=sys.stderr)
        sys.exit(1)

    from ferrum.errors import FerrumMigrationError
    from ferrum.migrations import apply

    if do_dry_run:
        # Dry-run does not need a DB connection; pass a sentinel conn object.
        # apply() with dry_run=True never calls conn._require_pool().
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
            print(f"Error: {exc}", file=sys.stderr)
            sys.exit(1)
        status = "applied"

    print(f"[ferrum migrate] {status} {result.ops_count} ops")
