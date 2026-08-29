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

Class-level ``dependencies``, ``operations``, and ``reverse_operations`` are
overridden by subclasses; the defaults here are empty so a bare ``Migration``
subclass is always valid.

An empty ``reverse_operations`` list marks the migration as irreversible.
``ferrum revert`` will refuse to revert any migration whose
``reverse_operations`` is empty.

Reversibility contract (W3-A):
- ``is_reversible(migration_cls)`` returns ``True`` iff ``reverse_operations``
  is non-empty.
- ``reverse_classifications(migration_cls)`` returns the classification
  (``"safe"`` / ``"destructive"`` / ``"non_transactional"``) for each reverse
  operation, in declared order. The CLI ``revert`` path uses this to decide
  whether ``--confirm`` is required.
- Data-migration callables that need to run alongside DDL must subclass
  :class:`DataMigration` (defined in ``orchestrator.py``) and declare a
  ``transaction_policy``. They are never imported or executed automatically
  from untrusted files — only developer-authored migration modules may
  register them.
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from ferrum.migrations.operations import Operation


class Migration:
    """Base class for all Ferrum migration definitions."""

    dependencies: ClassVar[list[str]] = []
    operations: ClassVar[list[Operation]] = []
    reverse_operations: ClassVar[list[Operation]] = []  # empty = irreversible

    @classmethod
    def get_name(cls, file_path: str) -> str:
        """Return the migration name derived from the file stem.

        Example: ``"migrations/0001_create_note.py"`` → ``"0001_create_note"``.
        """
        return Path(file_path).stem


def is_reversible(migration_cls: type[Migration]) -> bool:
    """Return ``True`` iff *migration_cls* declares non-empty ``reverse_operations``.

    A migration with empty ``reverse_operations`` is irreversible: ``ferrum revert``
    refuses to run it. This helper lets the orchestrator graph and the CLI share
    one definition of reversibility.
    """
    return len(migration_cls.reverse_operations) > 0


def reverse_classifications(migration_cls: type[Migration]) -> list[str]:
    """Return the classification of each reverse operation in declared order.

    The returned strings are exactly the values of
    :attr:`Operation.classification` (``"safe"``, ``"destructive"``, or
    ``"non_transactional"``). The CLI revert path uses this list to decide
    whether ``--confirm`` is required: any ``"destructive"`` entry forces the
    confirmation gate.
    """
    return [op.classification for op in migration_cls.reverse_operations]
