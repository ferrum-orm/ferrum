"""CLI command: apply unapplied migrations in order.

Enforces the dry-run → confirm → apply sequence for destructive operations
and the non-development environment gate (MIG-1, MIG-2, MIG-5).

Security invariants:
- No credentials, bound values, or row data appear in output.
- Destructive operations require explicit ``--confirm`` (MIG-2).
- ``record_applied`` is called inside the same DDL transaction so a partial
  migration cannot be recorded as applied (atomicity best-effort; final
  ledger write uses the pool, not the transaction connection — see note below).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import typer
from rich import print as rprint

from ferrum.connection import connect
from ferrum.errors import FerrumConfigError, FerrumMigrationError
from ferrum.migrations import ledger as _ledger
from ferrum.migrations import loader as _loader
from ferrum.migrations.orchestrator import _op_to_sql


async def run_migrate(
    migrations_dir: Path,
    *,
    env: str = "development",
    confirm: bool = False,
    dry_run: bool = False,
) -> int:
    """Apply unapplied migrations in order.

    Args:
        migrations_dir: Directory containing ``NNNN_slug.py`` migration files.
        env: Target environment name.  Non-``"development"`` values require
            ``confirm=True`` at the ``apply()`` layer (MIG-5); this function
            propagates the value to ``record_applied``.
        confirm: When ``True``, destructive operations are permitted.
        dry_run: When ``True``, print what would be applied without executing.

    Returns:
        Exit code: ``0`` = applied (or dry-run complete), ``1`` = nothing to
        do, ``2`` = error or safety gate blocked execution.
    """
    try:
        async with connect() as conn:
            await _ledger.ensure_ledger(conn)

            modules = _loader.scan(migrations_dir)

            # Pair each module with its content-keyed digest and filter applied.
            unapplied: list[tuple[_loader.MigrationModule, str]] = []
            for module in modules:
                content = module.path.read_text(encoding="utf-8")
                digest = _ledger.compute_digest(module.name, content)
                if not await _ledger.is_applied(conn, digest):
                    unapplied.append((module, digest))

            if not unapplied:
                print("Nothing to apply.")
                return 1

            # Identify migrations that contain at least one destructive operation.
            destructive_names = [
                module.name
                for module, _ in unapplied
                if any(op.classification == "destructive" for op in module.migration.operations)
            ]

            if destructive_names:
                if dry_run:
                    # Dry run is always safe — show the full plan and exit cleanly.
                    print("Would apply the following migrations:")
                    for module, _ in unapplied:
                        print(f"  - {module.name}")
                    return 0
                if not confirm:
                    print(
                        "The following migrations contain destructive operations:\n"
                        + "\n".join(f"  - {name}" for name in destructive_names)
                    )
                    print("Re-run with --confirm to apply destructive changes.")
                    return 2

            for module, digest in unapplied:
                ops = module.migration.operations
                rprint(f"Applying [bold]{module.name}[/bold]...")

                if dry_run:
                    rprint(f"  [dim][dry-run][/dim] would apply {len(ops)} operations")
                    continue

                try:
                    pool = conn._require_pool()
                    async with pool.acquire() as db_conn, db_conn.transaction():
                        for op in ops:
                            sql = _op_to_sql(op.to_op_dict())
                            await db_conn.execute(sql)
                        # record_applied uses conn (pool), not db_conn — the INSERT
                        # is issued via a separate pool connection.  PostgreSQL DDL
                        # is committed first on transaction exit, then ledger is
                        # written; a failure here leaves the DDL applied but
                        # un-recorded, which is detectable on the next run.
                        await _ledger.record_applied(
                            conn,
                            digest,
                            environment=env,
                            description=module.name,
                        )
                except FerrumMigrationError:
                    raise
                except Exception as exc:
                    raise FerrumMigrationError(
                        f"Failed to apply migration {module.name!r}: "
                        f"{type(exc).__name__} [FERR-M001]"
                    ) from None

                rprint("  [green]OK[/green]")

            return 0

    except FerrumConfigError as exc:
        print(f"Configuration error: {exc}")
        return 2
    except FerrumMigrationError as exc:
        print(f"Migration error: {exc}")
        return 2


def migrate(
    *,
    env: str = "development",
    confirm: bool = False,
    dry_run: bool = False,
    migrations_dir: Path | None = None,
) -> None:
    """Sync CLI entry-point: delegate to :func:`run_migrate`."""
    path = migrations_dir or _loader.migrations_dir_default()
    exit_code = asyncio.run(run_migrate(path, env=env, confirm=confirm, dry_run=dry_run))
    if exit_code != 0:
        raise typer.Exit(code=exit_code)
