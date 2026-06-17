"""``ferrum showmigrations`` command: list migrations with applied/pending status.

Renders a Rich table with one row per migration file in dependency order.

Always exits 0 — this is an informational command.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from rich.console import Console
from rich.table import Table

from ferrum.connection import connect
from ferrum.errors import FerrumConfigError
from ferrum.migrations import ledger, loader


async def run_showmigrations(migrations_dir: Path) -> int:
    """Print migration status: [X] applied, [ ] pending.

    Returns 0 always (informational command).
    """
    modules = loader.scan(migrations_dir)
    console = Console()
    if not modules:
        console.print("No migrations found.")
        return 0

    table = Table(show_header=True, header_style="bold")
    table.add_column("Status", style="dim", width=8)
    table.add_column("Migration")

    try:
        async with connect() as conn:
            await ledger.ensure_ledger(conn)
            for module in modules:
                digest = ledger.compute_digest(module.name, module.path.read_text(encoding="utf-8"))
                applied = await ledger.is_applied(conn, digest)
                if applied:
                    table.add_row("[green][X][/green]", module.name)
                else:
                    table.add_row("[yellow][ ][/yellow]", module.name)
    except FerrumConfigError:
        for module in modules:
            table.add_row("[dim][?][/dim]", f"{module.name}  (no database connection)")

    console.print(table)
    return 0


def showmigrations(*, migrations_dir: Path | None = None) -> None:
    """Sync CLI entry-point: delegate to :func:`run_showmigrations`."""
    path = migrations_dir or loader.migrations_dir_default()
    asyncio.run(run_showmigrations(path))
