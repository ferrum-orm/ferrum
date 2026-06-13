"""Migration orchestrator: dry-run, plan classification, apply sequencing.

The orchestrator is the entry point for all migration operations. It enforces
the mandatory dry-run → confirm → apply sequence (MIG-1) and routes plans
through the appropriate gate checks (MIG-2 / MIG-5) before any SQL reaches
the database.

No SQL is applied without a completed dry-run cycle. This is enforced
structurally: ``apply()`` requires the ``MigrationPlan`` object returned by
``dry_run()``, not raw SQL strings.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class OperationClass(Enum):
    """Classification of a migration operation by safety profile."""

    SAFE = "safe"
    DESTRUCTIVE = "destructive"
    NON_TRANSACTIONAL = "non_transactional"


@dataclass
class PlannedOperation:
    sql: str
    description: str
    classification: OperationClass
    table: str = ""


@dataclass
class MigrationPlan:
    """The output of a dry-run pass. Required as input to ``apply()``."""

    operations: list[PlannedOperation] = field(default_factory=list)
    digest: str = ""
    dry_run_completed: bool = False
    has_destructive: bool = False

    def __post_init__(self) -> None:
        self.has_destructive = any(
            op.classification == OperationClass.DESTRUCTIVE for op in self.operations
        )


async def dry_run(
    current_schema: dict[str, Any],
    target_schema: dict[str, Any],
) -> MigrationPlan:
    """Compute the migration plan without touching the database.

    Returns a ``MigrationPlan`` that can be inspected and passed to ``apply()``.
    Raises ``FerrumMigrationError`` if the plan cannot be computed.
    """
    raise NotImplementedError("dry_run() implementation pending schema-diff landing")


async def apply(
    plan: MigrationPlan,
    *,
    confirmation_token: str | None = None,
    environment: str = "development",
) -> None:
    """Apply a pre-computed migration plan to the database.

    Args:
        plan: Must be the output of a completed ``dry_run()`` (MIG-1).
        confirmation_token: Required for destructive operations (MIG-2).
            Tokens are single-use and expire after apply (MIG-8).
        environment: Non-development environments require explicit confirmation
            before destructive operations proceed (MIG-5).

    Raises:
        FerrumMigrationError: Dry-run not completed, token missing/invalid/replayed,
            or environment confirmation absent.
    """
    if not plan.dry_run_completed:
        from ferrum.errors import FerrumMigrationError

        raise FerrumMigrationError(
            "Cannot apply migration: dry_run() has not been completed. "
            "Call dry_run() first and pass the returned plan to apply()."
        )
    raise NotImplementedError("apply() implementation pending connection layer")
