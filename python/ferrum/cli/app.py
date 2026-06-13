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

    # ferrum migrations
    mig_parser = subparsers.add_parser("migrations", help="Migration commands")
    mig_sub = mig_parser.add_subparsers(dest="mig_command")
    mig_sub.add_parser("dry-run", help="Compute and display a migration plan")
    apply_parser = mig_sub.add_parser("apply", help="Apply a migration plan")
    apply_parser.add_argument("--token", help="Confirmation token for destructive operations")
    apply_parser.add_argument("--environment", default="development", help="Target environment")

    args = parser.parse_args()

    if args.command == "init":
        from ferrum.cli.init import run_init
        run_init(name=args.name)
    elif args.command == "migrations":
        from ferrum.cli.migrations_cmd import run_migrations
        run_migrations(args)
    else:
        parser.print_help()
        sys.exit(0)
