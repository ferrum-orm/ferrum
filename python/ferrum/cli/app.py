"""CLI application definition using argparse (no typer dep for base CLI).

Typer/rich are optional extras (``ferrum[cli]``); the base CLI works on stdlib
alone so that ``ferrum`` core stays dependency-light (PROJECT_STRUCTURE.md §4.2).
"""

from __future__ import annotations

import argparse
import sys


def app() -> None:
    """Main CLI dispatcher."""
    parser = argparse.ArgumentParser(
        prog="ferrum",
        description="Ferrum ORM CLI",
    )
    subparsers = parser.add_subparsers(dest="command", metavar="<command>")

    # ferrum init
    init_parser = subparsers.add_parser("init", help="Scaffold a new Ferrum project")
    init_parser.add_argument(
        "--name",
        default=".",
        help="Project name / directory (default: current directory)",
    )

    # ferrum migrations  (legacy JSON escape hatch: dry-run and apply via plan file)
    mig_parser = subparsers.add_parser(
        "migrations", help="Migration commands (legacy plan-file API)"
    )
    mig_sub = mig_parser.add_subparsers(dest="mig_command")
    dryr_parser = mig_sub.add_parser("dry-run", help="Dry-run a migration plan JSON file")
    dryr_parser.add_argument(
        "plan_file",
        nargs="?",
        default=None,
        help="Path to migration plan JSON file (produced by Rust core)",
    )
    dryr_parser.add_argument(
        "--environment",
        default="development",
        help="Target environment",
    )
    apply_parser = mig_sub.add_parser("apply", help="Apply a migration plan")
    apply_parser.add_argument(
        "plan_file",
        nargs="?",
        default=None,
        help="Path to migration plan JSON file (produced by Rust core)",
    )
    apply_parser.add_argument(
        "--token",
        help="Confirmation token for destructive operations (MIG-2)",
    )
    apply_parser.add_argument(
        "--confirm",
        action="store_true",
        default=False,
        help="Confirm destructive operations and non-development applies",
    )
    apply_parser.add_argument(
        "--dry-run",
        dest="do_dry_run",
        action="store_true",
        default=False,
        help="Print the plan without applying it (always safe)",
    )
    apply_parser.add_argument(
        "--environment",
        default="development",
        help="Target environment (non-development requires --confirm)",
    )

    # ferrum makemigrations
    makemig_parser = subparsers.add_parser(
        "makemigrations", help="Generate migration files from model state"
    )
    makemig_parser.add_argument(
        "--name",
        default=None,
        help="Optional slug for the migration file name (default: auto)",
    )
    makemig_parser.add_argument(
        "--migrations-dir",
        dest="migrations_dir",
        default=None,
        help="Migrations directory (default: ./migrations)",
    )

    # ferrum migrate
    migrate_parser = subparsers.add_parser("migrate", help="Apply unapplied migrations in order")
    migrate_parser.add_argument(
        "--env",
        default="development",
        help="Target environment (default: development)",
    )
    migrate_parser.add_argument(
        "--confirm",
        action="store_true",
        default=False,
        help="Confirm destructive operations",
    )
    migrate_parser.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        default=False,
        help="Show what would be applied without applying",
    )
    migrate_parser.add_argument(
        "--migrations-dir",
        dest="migrations_dir",
        default=None,
        help="Migrations directory (default: ./migrations)",
    )

    # ferrum showmigrations
    showmig_parser = subparsers.add_parser(
        "showmigrations", help="List migrations with applied/pending status"
    )
    showmig_parser.add_argument(
        "--migrations-dir",
        dest="migrations_dir",
        default=None,
        help="Migrations directory (default: ./migrations)",
    )

    args = parser.parse_args()

    if args.command == "init":
        from ferrum.cli.init import run_init

        run_init(name=args.name)
    elif args.command == "migrations":
        from ferrum.cli.migrations_cmd import run_migrations

        run_migrations(args)
    elif args.command == "makemigrations":
        from ferrum.cli.makemigrations_cmd import dispatch_makemigrations

        dispatch_makemigrations(args)
    elif args.command == "migrate":
        from ferrum.cli.migrate_cmd import dispatch_migrate

        dispatch_migrate(args)
    elif args.command == "showmigrations":
        from ferrum.cli.showmigrations_cmd import dispatch_showmigrations

        dispatch_showmigrations(args)
    else:
        parser.print_help()
        sys.exit(0)
