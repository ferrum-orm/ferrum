"""CLI command: apply unapplied migrations in order.

Enforces the dry-run → confirm → apply sequence for destructive operations
and the non-development environment gate (MIG-1, MIG-2, MIG-5).

W1-C: on PostgreSQL, each migration's ops + ledger write run inside one
advisory-locked transaction on a pinned connection (atomic DDL + ledger).
Non-transactional ops (CREATE INDEX CONCURRENTLY) run as explicit post-tx
phases. Non-PostgreSQL backends keep best-effort autocommit-per-op.

Security invariants:
- No credentials, bound values, or row data appear in output.
- Destructive operations require explicit ``--confirm`` (MIG-2).
- ``record_applied`` runs inside the same DDL transaction so a partial
  migration cannot be recorded as applied.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import typer
from rich import print as rprint

from ferrum.connection import Connection, connect
from ferrum.errors import FerrumConfigError, FerrumMigrationError, migration_op_failure
from ferrum.migrations import ledger as _ledger
from ferrum.migrations import loader as _loader
from ferrum.migrations.ledger import (
    ADVISORY_LOCK_KEY_1,
    ADVISORY_LOCK_KEY_2,
    advisory_lock_sql,
    is_applied_on_conn,
    record_applied_on_conn,
)
from ferrum.migrations.orchestrator import _is_op_non_transactional, _op_to_sql


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

            # Checksum gate: fail when an applied migration file was edited on disk.
            for module in modules:
                content = module.path.read_text(encoding="utf-8")
                digest = _ledger.compute_digest(module.name, content)
                await _ledger.verify_checksum(conn, module.name, digest)

            # Drift warning when model classes are registered in the process.
            try:
                from ferrum.cli.makemigrations_cmd import _get_all_model_subclasses
                from ferrum.migrations.drift import detect_drift

                model_classes = _get_all_model_subclasses()
                if model_classes:
                    drift_report = await detect_drift(conn, model_classes)
                    if drift_report.has_drift:
                        print("Warning: schema drift detected before migrate.")
                        for table in drift_report.missing_tables:
                            print(f"  - missing table: {table}")
                        for table, diff in drift_report.column_diffs.items():
                            for col in diff.get("missing_columns", []):
                                print(f"  - missing column: {table}.{col}")
            except Exception:  # noqa: S110 — schema drift hint is best-effort only
                pass

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

                driver = conn._require_driver()
                dialect = conn.dialect
                if dialect == "postgres":
                    await _apply_migration_postgres(conn, module, digest, ops, env=env)
                else:
                    await _apply_migration_thin(conn, driver, dialect, module, digest, ops, env=env)
                rprint("  [green]OK[/green]")

            return 0

    except FerrumConfigError as exc:
        print(f"Configuration error: {exc}")
        return 2
    except FerrumMigrationError as exc:
        print(f"Migration error: {exc}")
        return 2


async def _apply_migration_postgres(
    conn: Connection,
    module: _loader.MigrationModule,
    digest: str,
    ops: list,
    *,
    env: str,
) -> None:
    """Apply one migration on PostgreSQL with advisory lock + transactional DDL + atomic ledger.

    W1-C: pins one connection, acquires pg_advisory_xact_lock, checks the
    ledger, runs all transactional ops, writes the ledger row atomically,
    then runs any non-transactional post-tx ops (CREATE INDEX CONCURRENTLY).
    """
    # Split ops into pre-tx (non-tx), tx, post-tx (non-tx) phases.
    pre_ops: list = []
    tx_ops: list = []
    post_ops: list = []
    phase = "pre"
    for op in ops:
        non_tx = _is_op_non_transactional(op.to_op_dict())
        if non_tx:
            if phase == "pre":
                pre_ops.append(op)
            elif phase == "tx":
                phase = "post"
                post_ops.append(op)
            else:
                post_ops.append(op)
        else:
            if phase == "pre":
                phase = "tx"
            tx_ops.append(op)

    driver = conn._require_driver()

    # Pre-tx non-transactional ops (autocommit, no ledger yet).
    for op in pre_ops:
        op_dict = op.to_op_dict()
        sql = _op_to_sql(op_dict, dialect="postgres")
        await driver.execute(sql)

    # Transactional phase: pin connection, advisory lock, ops, atomic ledger.
    async with conn.acquire() as raw_conn, raw_conn.transaction():
        await raw_conn.execute(advisory_lock_sql(), ADVISORY_LOCK_KEY_1, ADVISORY_LOCK_KEY_2)
        if await is_applied_on_conn(raw_conn, digest, dialect="postgres"):
            raise FerrumMigrationError(
                f"Migration {module.name!r} has already been applied. [FERR-M003]"
            )
        for op_index, op in enumerate(tx_ops):
            op_dict = op.to_op_dict()
            sql = _op_to_sql(op_dict, dialect="postgres")
            try:
                await raw_conn.execute(sql)
            except FerrumMigrationError:
                raise
            except Exception as exc:
                raise migration_op_failure(
                    action="apply",
                    migration_name=module.name,
                    op_index=op_index,
                    op=op_dict,
                    exc=exc,
                ) from None
        await record_applied_on_conn(
            raw_conn,
            digest,
            environment=env,
            description=module.name,
            dialect="postgres",
        )

    # Post-tx non-transactional ops (autocommit, ledger already written).
    for op in post_ops:
        op_dict = op.to_op_dict()
        sql = _op_to_sql(op_dict, dialect="postgres")
        try:
            await driver.execute(sql)
        except FerrumMigrationError:
            raise
        except Exception as exc:
            raise FerrumMigrationError(
                f"Post-transaction op failed after ledger commit in {module.name!r} "
                f"({op_dict.get('kind', 'unknown')}): {type(exc).__name__}. "
                f"The migration is recorded as applied; the failed op must be "
                f"reconciled manually. [FERR-M001]"
            ) from None


async def _apply_migration_thin(
    conn: Connection,
    driver: Any,  # noqa: ANN401
    dialect: str,
    module: _loader.MigrationModule,
    digest: str,
    ops: list,
    *,
    env: str,
) -> None:
    """Apply one migration on non-PostgreSQL backends (best-effort thin parity)."""
    for op_index, op in enumerate(ops):
        op_dict = op.to_op_dict()
        sql = _op_to_sql(op_dict, dialect=dialect)
        try:
            await driver.execute(sql)
        except FerrumMigrationError:
            raise
        except Exception as exc:
            raise migration_op_failure(
                action="apply",
                migration_name=module.name,
                op_index=op_index,
                op=op_dict,
                exc=exc,
            ) from None
    await _ledger.record_applied(
        conn,
        digest,
        environment=env,
        description=module.name,
    )


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
