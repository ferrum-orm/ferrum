"""Migration safety gates.

Gates enforce pre-conditions before destructive SQL is applied:
- Destructive operations (column drop, table drop, type narrowing, NOT NULL on
  populated column) require a valid confirmation token (MIG-2).
- Non-development environments require an explicit environment name to be passed
  in, matching the target (MIG-5).

Gate checks run inside the orchestrator, before any SQL reaches asyncpg.
"""

from __future__ import annotations

from ferrum.errors import FerrumMigrationError
from ferrum.migrations.tokens import validate_token

_NON_DEV_ENVIRONMENTS = frozenset({"staging", "production", "prod", "stg"})


def check_destructive_gate(
    plan_digest: str,
    confirmation_token: str | None,
) -> None:
    """Raise if a destructive plan lacks a valid confirmation token (MIG-2).

    Args:
        plan_digest: The canonical digest of the migration plan.
        confirmation_token: The operator-supplied token. Must be bound to ``plan_digest``.

    Raises:
        FerrumMigrationError: Token absent, malformed, or does not match the plan.
    """
    if confirmation_token is None:
        raise FerrumMigrationError(
            "This migration contains destructive operations (column drop, table drop, "
            "type narrowing, or NOT NULL on a populated column). "
            "A confirmation token is required. Generate one with "
            "`ferrum migrations token` and pass it via --token or FERRUM_MIGRATION_TOKEN."
        )
    if not validate_token(confirmation_token, plan_digest):
        raise FerrumMigrationError(
            "Confirmation token does not match the migration plan digest. "
            "Regenerate the token for this specific plan."
        )


def check_environment_gate(
    declared_environment: str,
    target_environment: str,
) -> None:
    """Raise if the environment declaration doesn't match the target (MIG-5).

    Non-development applies require an explicit ``--environment`` flag matching
    the actual target environment so operators cannot accidentally apply a
    production migration to staging or vice versa.

    Args:
        declared_environment: What the operator said (e.g. via ``--environment prod``).
        target_environment: The environment the connection is configured for.

    Raises:
        FerrumMigrationError: Declaration absent or mismatched.
    """
    if target_environment in _NON_DEV_ENVIRONMENTS:
        if declared_environment != target_environment:
            raise FerrumMigrationError(
                f"Applying to '{target_environment}' requires "
                f"--environment {target_environment} to be passed explicitly. "
                f"Got: '{declared_environment}'. "
                "This guard prevents accidental cross-environment applies."
            )
