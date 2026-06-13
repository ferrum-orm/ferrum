"""``ferrum migrations`` subcommands: dry-run and apply.

Wraps ``ferrum.migrations.orchestrator`` with CLI-friendly output and token
handling. Tokens are read from ``--token`` or ``FERRUM_MIGRATION_TOKEN``
environment variable — never from argv positional arguments (MIG-7).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys


def run_migrations(args: argparse.Namespace) -> None:
    """Dispatch to the appropriate migration subcommand."""
    cmd = getattr(args, "mig_command", None)
    if cmd == "dry-run":
        asyncio.run(_dry_run())
    elif cmd == "apply":
        token = args.token or os.environ.get("FERRUM_MIGRATION_TOKEN")
        asyncio.run(_apply(token=token, environment=args.environment))
    else:
        print("Usage: ferrum migrations <dry-run | apply>")
        sys.exit(1)


async def _dry_run() -> None:
    print("Migration dry-run: not yet implemented (connection layer pending)")


async def _apply(*, token: str | None, environment: str) -> None:
    print("Migration apply: not yet implemented (connection layer pending)")
