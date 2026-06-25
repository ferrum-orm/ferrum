"""``ferrum sqlmigrate`` command: render offline SQL for a migration file.

No database connection is required. SQL is produced via the same allowlisted
``_op_to_sql`` path used by ``ferrum migrate``.
"""

from __future__ import annotations

from pathlib import Path

from ferrum.migrations import loader as _loader
from ferrum.migrations.orchestrator import _op_to_sql


def run_sqlmigrate(
    migrations_dir: Path,
    migration_name: str,
    *,
    dialect: str = "postgres",
) -> int:
    """Print offline SQL statements for *migration_name*.

    Returns:
        ``0`` on success, ``2`` when the migration is not found.
    """
    modules = _loader.scan(migrations_dir)
    target = next((m for m in modules if m.name == migration_name), None)
    if target is None:
        print(f"Migration {migration_name!r} not found.")
        return 2

    for op in target.migration.operations:
        op_dict = op.to_op_dict()
        sql = _op_to_sql(op_dict, dialect=dialect)
        print(f"-- {op_dict.get('kind', 'unknown')}")
        print(f"{sql};")
        print()
    return 0


def sqlmigrate(
    migration_name: str,
    *,
    migrations_dir: Path | None = None,
    dialect: str = "postgres",
) -> None:
    """Sync CLI entry-point: delegate to :func:`run_sqlmigrate`."""
    import typer

    path = migrations_dir or _loader.migrations_dir_default()
    exit_code = run_sqlmigrate(path, migration_name, dialect=dialect)
    if exit_code != 0:
        raise typer.Exit(code=exit_code)
