"""CLI application definition using Typer (requires ``ferrum[cli]`` extra)."""

from __future__ import annotations

from pathlib import Path

import typer

cli = typer.Typer(
    name="ferrum",
    help="Ferrum ORM CLI",
    no_args_is_help=True,
)

migrations_app = typer.Typer(
    help="Migration commands (legacy plan-file API)",
    no_args_is_help=True,
)
cli.add_typer(migrations_app, name="migrations")


@cli.command("init")
def init_cmd(
    name: str = typer.Option(
        ".",
        "--name",
        help="Project name / directory (default: current directory)",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Overwrite existing scaffold files (default: skip existing)",
    ),
) -> None:
    """Scaffold a new Ferrum project."""
    from ferrum.cli.init import run_init

    run_init(name=name, force=force)


@cli.command("makemigrations")
def makemigrations_cmd(
    name: str | None = typer.Option(
        None,
        "--name",
        help="Optional slug for the migration file name (default: auto)",
    ),
    migrations_dir: Path | None = typer.Option(
        None,
        "--migrations-dir",
        help="Migrations directory (default: ./migrations)",
    ),
) -> None:
    """Generate migration files from model state."""
    from ferrum.cli.makemigrations_cmd import makemigrations

    makemigrations(name=name, migrations_dir=migrations_dir)


@cli.command("migrate")
def migrate_cmd(
    env: str = typer.Option(
        "development",
        "--env",
        help="Target environment (default: development)",
    ),
    confirm: bool = typer.Option(
        False,
        "--confirm",
        help="Confirm destructive operations",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Show what would be applied without applying",
    ),
    migrations_dir: Path | None = typer.Option(
        None,
        "--migrations-dir",
        help="Migrations directory (default: ./migrations)",
    ),
) -> None:
    """Apply unapplied migrations in order."""
    from ferrum.cli.migrate_cmd import migrate

    migrate(env=env, confirm=confirm, dry_run=dry_run, migrations_dir=migrations_dir)


@cli.command("inspectdb")
def inspectdb_cmd(
    output: Path | None = typer.Option(
        None,
        "--output",
        "-o",
        help="Output file or directory (default: stdout)",
    ),
    schema: str = typer.Option(
        "public",
        "--schema",
        help="PostgreSQL schema to introspect",
    ),
) -> None:
    """Generate Ferrum model definitions from an existing database schema."""
    from ferrum.cli.inspectdb_cmd import dispatch_inspectdb

    dispatch_inspectdb(output=output, schema=schema)


@cli.command("revert")
def revert_cmd(
    target: str | None = typer.Option(
        None,
        "--target",
        help="Revert down to (exclusive) this migration name",
    ),
    env: str = typer.Option(
        "development",
        "--env",
        help="Target environment (default: development)",
    ),
    confirm: bool = typer.Option(
        False,
        "--confirm",
        help="Confirm destructive reverse operations",
    ),
    migrations_dir: Path | None = typer.Option(
        None,
        "--migrations-dir",
        help="Migrations directory (default: ./migrations)",
    ),
) -> None:
    """Revert the last applied migration (or down to --target)."""
    from ferrum.cli.revert_cmd import dispatch_revert

    dispatch_revert(target=target, env=env, confirm=confirm, migrations_dir=migrations_dir)


@cli.command("sqlmigrate")
def sqlmigrate_cmd(
    migration_name: str = typer.Argument(..., help="Migration name (e.g. 0001_create_note)"),
    migrations_dir: Path | None = typer.Option(
        None,
        "--migrations-dir",
        help="Migrations directory (default: ./migrations)",
    ),
    dialect: str = typer.Option(
        "postgres",
        "--dialect",
        help="SQL dialect for offline rendering (default: postgres)",
    ),
) -> None:
    """Print offline SQL for a migration (no database connection)."""
    from ferrum.cli.sqlmigrate_cmd import sqlmigrate

    sqlmigrate(migration_name, migrations_dir=migrations_dir, dialect=dialect)


@cli.command("showmigrations")
def showmigrations_cmd(
    migrations_dir: Path | None = typer.Option(
        None,
        "--migrations-dir",
        help="Migrations directory (default: ./migrations)",
    ),
) -> None:
    """List migrations with applied/pending status."""
    from ferrum.cli.showmigrations_cmd import showmigrations

    showmigrations(migrations_dir=migrations_dir)


@migrations_app.command("dry-run")
def migrations_dry_run_cmd(
    plan_file: Path | None = typer.Argument(
        None,
        help="Path to migration plan JSON file (produced by Rust core)",
    ),
    environment: str = typer.Option(
        "development",
        "--environment",
        help="Target environment",
    ),
) -> None:
    """Dry-run a migration plan JSON file."""
    from ferrum.cli.migrations_cmd import migrations_dry_run

    migrations_dry_run(plan_file=plan_file, environment=environment)


@migrations_app.command("apply")
def migrations_apply_cmd(
    plan_file: Path | None = typer.Argument(
        None,
        help="Path to migration plan JSON file (produced by Rust core)",
    ),
    token: str | None = typer.Option(
        None,
        "--token",
        help="Confirmation token for destructive operations (MIG-2)",
    ),
    confirm: bool = typer.Option(
        False,
        "--confirm",
        help="Confirm destructive operations and non-development applies",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Print the plan without applying it (always safe)",
    ),
    environment: str = typer.Option(
        "development",
        "--environment",
        help="Target environment (non-development requires --confirm)",
    ),
) -> None:
    """Apply a migration plan."""
    from ferrum.cli.migrations_cmd import migrations_apply

    migrations_apply(
        plan_file=plan_file,
        token=token,
        confirm=confirm,
        dry_run=dry_run,
        environment=environment,
    )


@cli.command("resetdb")
def resetdb_cmd(
    env: str = typer.Option("development", "--env"),
    confirm: bool = typer.Option(False, "--confirm", help="Required — confirm destructive reset"),
) -> None:
    """Drop all Ferrum model tables and clear the migration ledger. Requires --confirm."""
    from ferrum.cli.resetdb_cmd import dispatch_resetdb

    dispatch_resetdb(env=env, confirm=confirm)


@cli.command("check-schema")
def check_schema_cmd(
    schema: str = typer.Option(
        "public",
        "--schema",
        help="PostgreSQL schema to introspect (default: public)",
    ),
    exclude_tables: str | None = typer.Option(
        None,
        "--exclude-tables",
        help="Comma-separated extra table names to exclude from drift comparison",
    ),
    auth_tables: str | None = typer.Option(
        None,
        "--auth-tables",
        help=(
            "Comma-separated Better Auth table names (default: user, session, "
            "account, verification)"
        ),
    ),
    langgraph_tables: str | None = typer.Option(
        None,
        "--langgraph-tables",
        help=(
            "Comma-separated LangGraph checkpoint tables (default: checkpoints, "
            "checkpoint_writes, checkpoint_blobs, checkpoint_migrations)"
        ),
    ),
    alembic_tables: str | None = typer.Option(
        None,
        "--alembic-tables",
        help="Comma-separated Alembic version tables (default: alembic_version)",
    ),
    include_unmapped: bool = typer.Option(
        False,
        "--include-unmapped",
        help="Report live tables that have no registered Ferrum model (default: ignore)",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Emit machine-readable JSON for CI (default: human-readable summary)",
    ),
    expected_extensions: str | None = typer.Option(
        None,
        "--expected-extensions",
        help="Comma-separated extension names expected to be installed (e.g. pgvector,pg_trgm)",
    ),
) -> None:
    """Compare registered Ferrum models with the live PostgreSQL schema.

    Read-only: never emits or applies DDL. Numbered SQL migrations remain
    authoritative; this command only reports drift. Exit codes: 0 = clean,
    1 = drift, 2 = configuration/connection error.
    """
    from ferrum.cli.check_schema_cmd import dispatch_check_schema

    dispatch_check_schema(
        schema=schema,
        exclude_tables=exclude_tables,
        auth_tables=auth_tables,
        langgraph_tables=langgraph_tables,
        alembic_tables=alembic_tables,
        include_unmapped=include_unmapped,
        json_output=json_output,
        expected_extensions=expected_extensions,
    )


def app() -> None:
    """Invoke the Typer CLI (console-script entry after bootstrap)."""
    cli()
