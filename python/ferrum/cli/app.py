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
) -> None:
    """Scaffold a new Ferrum project."""
    from ferrum.cli.init import run_init

    run_init(name=name)


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


def app() -> None:
    """Invoke the Typer CLI (console-script entry after bootstrap)."""
    cli()
