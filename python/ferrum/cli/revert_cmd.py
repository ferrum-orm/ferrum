"""CLI command: revert applied migrations.

Enforces the destructive-op confirmation gate and the irreversibility gate
(``reverse_operations`` must be non-empty).

Security invariants:
- No credentials, bound values, or row data appear in output.
- Destructive reverse operations require explicit ``--confirm``.
- ``delete_applied`` is called after the DDL transaction commits.  A failure
  between DDL commit and ledger delete leaves the migration recorded as applied
  but with its schema effects rolled back; the next ``ferrum migrate`` run will
  surface the inconsistency via the digest check.
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


async def run_revert(
    migrations_dir: Path,
    *,
    target: str | None = None,
    env: str = "development",
    confirm: bool = False,
) -> int:
    """Revert applied migrations (last one, or down to --target exclusive).

    Args:
        migrations_dir: Directory containing ``NNNN_slug.py`` migration files.
        target: When given, revert all migrations applied *after* this name
            (exclusive).  The target migration itself is not reverted.
        env: Target environment name (propagated for future non-dev gate).
        confirm: When ``True``, destructive reverse operations are permitted.

    Returns:
        ``0`` — one or more migrations reverted successfully.
        ``1`` — target selection produced no migrations to revert.
        ``2`` — error or safety gate blocked execution.
    """
    try:
        async with connect() as conn:
            await _ledger.ensure_ledger(conn)

            modules = _loader.scan(migrations_dir)

            # Pair each module with its content-keyed digest; filter to applied.
            applied: list[tuple[_loader.MigrationModule, str]] = []
            for module in modules:
                content = module.path.read_text()
                digest = _ledger.compute_digest(module.name, content)
                if await _ledger.is_applied(conn, digest):
                    applied.append((module, digest))

            if not applied:
                print("No applied migrations to revert.")
                return 2

            if target is not None:
                target_idx: int | None = None
                for i, (mod, _) in enumerate(applied):
                    if mod.name == target:
                        target_idx = i
                        break
                if target_idx is None:
                    print(f"Target migration {target!r} not found among applied migrations.")
                    return 2
                # Revert all applied migrations strictly after target, most recent first.
                to_revert = list(reversed(applied[target_idx + 1 :]))
            else:
                # Default: revert only the most recently applied migration.
                to_revert = [applied[-1]]

            if not to_revert:
                print("Nothing to revert.")
                return 1

            for module, digest in to_revert:
                reverse_ops = module.migration.reverse_operations
                if not reverse_ops:
                    print(f"Migration {module.name!r} is irreversible (no reverse_operations).")
                    return 2

                destructive = [op for op in reverse_ops if op.classification == "destructive"]
                if destructive and not confirm:
                    print(
                        f"Migration {module.name!r} has destructive reverse operations.\n"
                        "Re-run with --confirm to revert destructive changes."
                    )
                    return 2

                rprint(f"Reverting [bold]{module.name}[/bold]...")

                try:
                    pool = conn._require_pool()
                    async with pool.acquire() as db_conn, db_conn.transaction():
                        for op in reverse_ops:
                            sql = _op_to_sql(op.to_op_dict())
                            await db_conn.execute(sql)
                    # Ledger delete is issued after the DDL transaction commits so
                    # the schema change and ledger removal are best-effort atomic.
                    await _ledger.delete_applied(conn, digest)
                except FerrumMigrationError:
                    raise
                except Exception as exc:
                    raise FerrumMigrationError(
                        f"Failed to revert migration {module.name!r}: "
                        f"{type(exc).__name__} [FERR-M004]"
                    ) from None

                rprint("  [green]OK[/green]")

            return 0

    except FerrumConfigError as exc:
        print(f"Configuration error: {exc}")
        return 2
    except FerrumMigrationError as exc:
        print(f"Migration error: {exc}")
        return 2


def dispatch_revert(
    *,
    target: str | None = None,
    env: str = "development",
    confirm: bool = False,
    migrations_dir: Path | None = None,
) -> None:
    """Sync CLI entry-point: delegate to :func:`run_revert`."""
    path = migrations_dir or _loader.migrations_dir_default()
    exit_code = asyncio.run(run_revert(path, target=target, env=env, confirm=confirm))
    if exit_code != 0:
        raise typer.Exit(code=exit_code)
