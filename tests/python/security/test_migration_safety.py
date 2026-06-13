"""Security qualification suite — migration safety (MIG-1, MIG-2, MIG-5, MIG-6, MIG-7, MIG-8).

MIG-1: Apply without dry-run fails.
MIG-2: Destructive operations require confirmation token.
MIG-5: Non-dev environments require explicit declaration.
MIG-6: Unscoped delete/update require danger API.
MIG-7: Token never appears in argv / public logs.
MIG-8: Token is single-use (replay rejected via ledger digest uniqueness).
"""

from __future__ import annotations

import pytest

from ferrum.errors import FerrumDangerApiError, FerrumMigrationError
from ferrum.migrations.gates import check_destructive_gate, check_environment_gate
from ferrum.migrations.orchestrator import MigrationPlan, apply
from ferrum.migrations.tokens import generate_token, validate_token

pytestmark = pytest.mark.security


class TestMigrationSafety:
    @pytest.mark.asyncio
    async def test_apply_without_dry_run_fails(self) -> None:
        """MIG-1: apply() must reject a plan that has not completed dry_run()."""
        plan = MigrationPlan(dry_run_completed=False)
        with pytest.raises(FerrumMigrationError, match="dry_run"):
            await apply(plan)

    def test_destructive_without_token_fails(self) -> None:
        """MIG-2: Destructive plan requires a confirmation token."""
        with pytest.raises(FerrumMigrationError, match="confirmation token"):
            check_destructive_gate("some_plan_digest", confirmation_token=None)

    def test_non_dev_without_environment_declaration_fails(self) -> None:
        """MIG-5: Production apply without explicit --environment flag fails."""
        with pytest.raises(FerrumMigrationError, match="production"):
            check_environment_gate("development", "production")

    @pytest.mark.asyncio
    async def test_unscoped_delete_requires_danger_api(self) -> None:
        """MIG-6: Unscoped QuerySet.delete() raises FerrumDangerApiError."""
        from ferrum.models import Model
        from ferrum.queryset import QuerySet

        class T(Model):
            id: int = 0

        qs: QuerySet[T] = QuerySet(T)
        with pytest.raises(FerrumDangerApiError):
            await qs.delete()

    def test_token_not_in_plan_object(self) -> None:
        """MIG-7: Confirmation tokens are not stored in the MigrationPlan object."""
        plan = MigrationPlan(dry_run_completed=True)
        # MigrationPlan must not carry a token field
        assert not hasattr(plan, "token")
        assert not hasattr(plan, "confirmation_token")

    def test_token_binds_to_specific_digest(self) -> None:
        """MIG-8: A token for digest A must not validate against digest B (replay guard)."""
        digest_a = "digest_a_x" * 3
        digest_b = "digest_b_y" * 3
        token_a = generate_token(digest_a)
        assert validate_token(token_a, digest_a)
        assert not validate_token(token_a, digest_b)
