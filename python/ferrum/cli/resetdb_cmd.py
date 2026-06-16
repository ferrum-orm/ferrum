"""CLI command: drop all Ferrum-managed tables and clear the migration ledger.

Security invariants:
- Requires explicit ``--confirm``; aborts with exit code 2 otherwise.
- Non-development environments print an additional safety warning before proceeding.
- Table names come exclusively from model metadata allowlists (``__ferrum_table__``),
  never from user input — they are quoted as PostgreSQL identifiers.
- No credentials, bound values, or row data appear in output.
"""

from __future__ import annotations

import asyncio

import typer

from ferrum.connection import connect
from ferrum.errors import FerrumConfigError
from ferrum.migrations.ledger import LEDGER_TABLE


def _get_all_model_table_names() -> list[str]:
    """Return table names for all concrete Model subclasses visible in the current process.

    Uses the same recursive ``__subclasses__()`` walk as ``makemigrations_cmd``.
    Only classes whose ``ModelMetadata`` was built are included.
    """
    from ferrum.models import Model

    seen: set[str] = set()
    tables: list[str] = []
    stack = list(Model.__subclasses__())
    while stack:
        cls = stack.pop()
        if getattr(cls, "__ferrum_metadata__", None) is not None:
            table = cls.__ferrum_table__
            if table and table not in seen:
                seen.add(table)
                tables.append(table)
        stack.extend(cls.__subclasses__())
    return tables


async def run_resetdb(*, env: str = "development", confirm: bool = False) -> int:
    """Drop all Ferrum-managed tables and clear the migration ledger.

    Args:
        env: Target environment name.  Non-``"development"`` values trigger an
            additional safety warning before proceeding.
        confirm: Must be ``True`` to proceed; if ``False`` this function prints a
            dry-run safety summary and returns exit code 2.

    Returns:
        Exit code: ``0`` = success, ``2`` = safety gate blocked or config error.
    """
    tables = _get_all_model_table_names()

    if not confirm:
        print("resetdb: destructive operation — would drop the following:")
        if tables:
            for t in tables:
                print(f"  table: {t}")
        else:
            print("  (no registered Ferrum model tables found)")
        print(f"  ledger: {LEDGER_TABLE} (TRUNCATE)")
        print()
        print("Re-run with --confirm to execute.")
        return 2

    if env != "development":
        print(
            f"WARNING: resetdb is running against environment '{env}'. "
            "This will permanently destroy all Ferrum model data. "
            "Proceeding because --confirm was passed."
        )

    try:
        async with connect() as conn:
            pool = conn._require_pool()

            dropped = 0
            for table in tables:
                await pool.execute(f'DROP TABLE IF EXISTS "{table}" CASCADE')
                dropped += 1

            await pool.execute(f"TRUNCATE {LEDGER_TABLE}")

    except FerrumConfigError as exc:
        print(f"Configuration error: {exc}")
        return 2

    print(f"Dropped {dropped} table(s).")
    print("Ledger cleared.")
    return 0


def dispatch_resetdb(*, env: str = "development", confirm: bool = False) -> None:
    """Sync CLI entry-point: delegate to :func:`run_resetdb`."""
    exit_code = asyncio.run(run_resetdb(env=env, confirm=confirm))
    if exit_code != 0:
        raise typer.Exit(code=exit_code)
