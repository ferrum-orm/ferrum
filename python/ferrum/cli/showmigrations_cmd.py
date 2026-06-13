"""``ferrum showmigrations`` command: list migrations with applied/pending status.

Prints one line per migration file in dependency order:

    [X] 0001_create_note        (applied)
    [ ] 0002_add_note_title     (pending)
    [?] 0003_drop_note_title    (no database connection)

Always exits 0 — this is an informational command.
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from ferrum.connection import connect
from ferrum.errors import FerrumConfigError
from ferrum.migrations import ledger, loader


async def run_showmigrations(migrations_dir: Path) -> int:
    """Print migration status: [X] applied, [ ] pending.

    Returns 0 always (informational command).
    """
    modules = loader.scan(migrations_dir)
    if not modules:
        print("No migrations found.")
        return 0

    try:
        async with connect() as conn:
            await ledger.ensure_ledger(conn)
            for module in modules:
                digest = ledger.compute_digest(module.name, module.path.read_text())
                applied = await ledger.is_applied(conn, digest)
                marker = "X" if applied else " "
                print(f"[{marker}] {module.name}")
    except FerrumConfigError:
        for module in modules:
            print(f"[?] {module.name}  (no database connection)")

    return 0


def dispatch_showmigrations(args: argparse.Namespace) -> None:
    """Sync CLI entry-point: parse *args* and delegate to :func:`run_showmigrations`.

    Args:
        args: Parsed arguments.  Expected attributes:

            - ``.migrations_dir`` (``str | None``): migrations directory;
              falls back to :func:`loader.migrations_dir_default`.
    """
    raw_dir = getattr(args, "migrations_dir", None)
    migrations_dir = Path(raw_dir) if raw_dir else loader.migrations_dir_default()
    asyncio.run(run_showmigrations(migrations_dir))
