"""Ferrum CLI entrypoint.

The ``ferrum`` console-script dispatches to subcommands defined in this package.
CLI modules must NOT import ``ferrum.cli`` or ``ferrum.contrib`` from core
query-path modules (enforced by import-linter in CI, PROJECT_STRUCTURE.md §6.4).
"""

from __future__ import annotations


def main() -> None:
    """Entry point for the ``ferrum`` CLI command."""
    from ferrum.cli.app import app
    app()
