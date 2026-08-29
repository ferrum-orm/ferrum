"""CLI command: ``ferrum check-schema`` — read-only schema-fidelity drift check.

Compares registered Ferrum model metadata with the live PostgreSQL schema
and reports differences. Numbered SQL migrations remain authoritative; this
command never emits or applies DDL.

Exit codes:
- ``0``: no drift detected
- ``1``: drift detected (CI failure)
- ``2``: configuration / connection error

Security invariants:
- No credentials, bound values, or row data appear in output.
- Schema and table identifiers come from explicit CLI options and the model
  metadata allowlist; never from untrusted input.
- Third-party tables (Better Auth, LangGraph, Alembic) are excluded by
  default and can be augmented via ``--exclude-table``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable

import typer

from ferrum.connection import connect
from ferrum.errors import FerrumConfigError
from ferrum.migrations.drift import (
    DriftReport,
    default_unmanaged_tables,
    detect_drift,
)

# Exit codes — kept as named constants so the register in app.py and tests
# can refer to them by meaning rather than magic numbers.
EXIT_CLEAN = 0
EXIT_DRIFT = 1
EXIT_CONFIG = 2


def _split_csv(value: str | None) -> tuple[str, ...]:
    """Split a comma-separated CLI value into a tuple of stripped names."""
    if value is None:
        return ()
    return tuple(part.strip() for part in value.split(",") if part.strip())


async def run_check_schema(
    *,
    schema: str = "public",
    exclude_tables: Iterable[str] = (),
    auth_tables: Iterable[str] = (),
    langgraph_tables: Iterable[str] = (),
    alembic_tables: Iterable[str] = (),
    include_unmapped: bool = False,
    json_output: bool = False,
    expected_extensions: Iterable[str] = (),
) -> int:
    """Execute the schema-fidelity drift check against the live database.

    Returns the exit code; never raises Ferrum-specific exceptions to the
    Typer layer (errors are printed and translated to ``EXIT_CONFIG``).
    """
    try:
        async with connect() as conn:
            if conn.dialect != "postgres":
                print("check-schema currently supports PostgreSQL only.")
                return EXIT_CONFIG
            report: DriftReport = await detect_drift(
                conn,
                None,
                schema=schema,
                exclude_tables=exclude_tables,
                auth_tables=auth_tables,
                langgraph_tables=langgraph_tables,
                alembic_tables=alembic_tables,
                include_unmapped_tables=include_unmapped,
                expected_extensions=expected_extensions,
            )
    except FerrumConfigError as exc:
        print(f"Configuration error: {exc}")
        return EXIT_CONFIG
    except Exception as exc:
        print(f"Connection error: {type(exc).__name__} [FERR-E101]")
        return EXIT_CONFIG

    if json_output:
        print(report.to_json())
    else:
        print(report.format_summary())
        if report.has_drift:
            print()
            print(
                "Re-run with --json for machine-readable output. Numbered SQL "
                "migrations remain authoritative."
            )
    return EXIT_DRIFT if report.has_drift else EXIT_CLEAN


def dispatch_check_schema(
    *,
    schema: str = "public",
    exclude_tables: str | None = None,
    auth_tables: str | None = None,
    langgraph_tables: str | None = None,
    alembic_tables: str | None = None,
    include_unmapped: bool = False,
    json_output: bool = False,
    expected_extensions: str | None = None,
) -> None:
    """Synchronous CLI entry-point: delegates to :func:`run_check_schema`."""
    # Default unmanaged tables are always excluded unless the caller
    # explicitly overrides a category. This keeps Better Auth / LangGraph /
    # Alembic coexistence painless.
    default_unmanaged = default_unmanaged_tables()
    auth_tuple = (
        _split_csv(auth_tables)
        if auth_tables is not None
        else tuple(
            t for t in default_unmanaged if t in ("user", "session", "account", "verification")
        )
    )
    langgraph_tuple = (
        _split_csv(langgraph_tables)
        if langgraph_tables is not None
        else tuple(t for t in default_unmanaged if t.startswith("checkpoint"))
    )
    alembic_tuple = (
        _split_csv(alembic_tables) if alembic_tables is not None else ("alembic_version",)
    )
    exclude_tuple = _split_csv(exclude_tables)
    extensions_tuple = _split_csv(expected_extensions)

    exit_code = asyncio.run(
        run_check_schema(
            schema=schema,
            exclude_tables=exclude_tuple,
            auth_tables=auth_tuple,
            langgraph_tables=langgraph_tuple,
            alembic_tables=alembic_tuple,
            include_unmapped=include_unmapped,
            json_output=json_output,
            expected_extensions=extensions_tuple,
        )
    )
    if exit_code != 0:
        raise typer.Exit(code=exit_code)
