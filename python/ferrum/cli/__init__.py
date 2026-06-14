"""Ferrum CLI entrypoint.

The ``ferrum`` console-script dispatches to subcommands defined in this package.
CLI modules must NOT import ``ferrum.cli`` or ``ferrum.contrib`` from core
query-path modules (enforced by import-linter in CI, PROJECT_STRUCTURE.md §6.4).
"""

from __future__ import annotations

import sys


def _require_cli_deps() -> None:
    """Fail fast when the optional ``ferrum[cli]`` extra is not installed."""
    try:
        import typer  # noqa: F401
    except ImportError:
        print("Ferrum CLI requires the [cli] extra. Install with: pip install 'ferrum[cli]'")
        sys.exit(1)


def main() -> None:
    """Entry point for the ``ferrum`` CLI command."""
    _require_cli_deps()

    from ferrum.cli.bootstrap import _bootstrap_project

    _bootstrap_project()

    from ferrum.cli.app import app

    app()
