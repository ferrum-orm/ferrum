"""Migration base class for Ferrum Django-style migration files.

Each migration file in the migrations directory should define a single
``Migration`` subclass.  Example::

    # migrations/0001_create_note.py
    from ferrum.migrations import Migration
    from ferrum.migrations import operations

    class Migration(Migration):
        dependencies = []

        operations = [
            operations.CreateTable("note", [
                operations.Column("id", "INTEGER", primary_key=True, not_null=True),
                operations.Column("body", "TEXT", not_null=True),
            ]),
        ]

Class-level ``dependencies`` and ``operations`` are overridden by subclasses;
the defaults here are empty so a bare ``Migration`` subclass is always valid.
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from ferrum.migrations.operations import Operation


class Migration:
    """Base class for all Ferrum migration definitions."""

    dependencies: ClassVar[list[str]] = []
    operations: ClassVar[list[Operation]] = []

    @classmethod
    def get_name(cls, file_path: str) -> str:
        """Return the migration name derived from the file stem.

        Example: ``"migrations/0001_create_note.py"`` → ``"0001_create_note"``.
        """
        return Path(file_path).stem
