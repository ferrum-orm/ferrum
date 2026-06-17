"""Security qualification tests for the migration orchestrator.

Covers AGENTS.md §3 migration safety gates:
- MIG-1: dry-run is mandatory default
- MIG-2: destructive ops require explicit confirmation
- MIG-3: non-dev applies require confirmation
- MIG-4: identifiers in generated SQL are double-quoted
- MIG-5: DSN/credentials never appear in migration dry-run output

Gate API: ``apply(conn, plan_json, *, dry_run=True, confirm=False, env="development")``.
``_op_to_sql(op_dict)`` emits DDL with all identifiers double-quoted (MIG-4).
Token lifecycle tests (MIG-6/MIG-7/MIG-8) are retained for the gate-function layer.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Skip entire module if ferrum.migrations hasn't landed yet (Track B guard).
# Currently the module exists; this documents the dependency and future-proofs
# environments where it might not yet be present.
pytest.importorskip("ferrum.migrations")

from ferrum.errors import FerrumDangerApiError, FerrumMigrationError
from ferrum.migrations.gates import check_destructive_gate, check_environment_gate
from ferrum.migrations.orchestrator import MigrationPlan, _op_to_sql, apply
from ferrum.migrations.tokens import generate_token, validate_token

pytestmark = pytest.mark.security


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _make_conn() -> MagicMock:
    """Return a mock connection whose pool.execute is an AsyncMock."""
    conn = MagicMock()
    pool = AsyncMock()
    conn._require_pool.return_value = pool
    return conn


def _plan_json(
    ops: list[dict],
    *,
    requires_confirmation: bool = False,
    name: str = "test_migration",
) -> str:
    return json.dumps(
        {
            "version": 1,
            "name": name,
            "requires_confirmation": requires_confirmation,
            "ops": ops,
        }
    )


def _create_table_ops() -> list[dict]:
    return [
        {
            "kind": "create_table",
            "table": "users",
            "columns": [
                {
                    "name": "id",
                    "sql_type": "BIGSERIAL",
                    "not_null": True,
                    "primary_key": True,
                }
            ],
        }
    ]


def _drop_table_ops() -> list[dict]:
    return [{"kind": "drop_table", "table": "users"}]


def _drop_column_ops() -> list[dict]:
    return [{"kind": "drop_column", "table": "users", "column": "email"}]


def _confirmation_token(plan_json: str) -> str:
    """Return the apply-path token expected by ``verify_token``."""
    return hashlib.sha256(plan_json.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# MIG-1: dry_run=True by default
# ---------------------------------------------------------------------------


class TestMIG1DryRunDefault:
    @pytest.mark.asyncio
    async def test_apply_dry_run_default_does_not_call_execute(self) -> None:
        """MIG-1: apply() with no kwargs defaults to dry_run=True; execute is never called."""
        conn = _make_conn()
        plan = _plan_json(_create_table_ops())
        result = await apply(conn, plan)
        conn._require_pool.return_value.execute.assert_not_called()
        assert result.dry_run is True
        assert result.applied is False

    @pytest.mark.asyncio
    async def test_apply_explicit_dry_run_true_does_not_call_execute(self) -> None:
        """MIG-1: explicit dry_run=True also suppresses DB execution."""
        conn = _make_conn()
        plan = _plan_json(_drop_table_ops(), requires_confirmation=True)
        result = await apply(conn, plan, dry_run=True)
        conn._require_pool.return_value.execute.assert_not_called()
        assert result.dry_run is True


# ---------------------------------------------------------------------------
# MIG-2: destructive ops require explicit confirmation
# ---------------------------------------------------------------------------


class TestMIG2DestructiveConfirmation:
    @pytest.mark.asyncio
    async def test_destructive_plan_without_confirm_raises(self) -> None:
        """MIG-2: DropTable plan with confirm=False, dry_run=False raises FerrumMigrationError."""
        conn = _make_conn()
        plan = _plan_json(_drop_table_ops(), requires_confirmation=True)
        with pytest.raises(FerrumMigrationError):
            await apply(conn, plan, dry_run=False, confirm=False)

    @pytest.mark.asyncio
    async def test_destructive_plan_with_dry_run_never_raises_for_confirmation(
        self,
    ) -> None:
        """MIG-2: DropTable plan with dry_run=True must not raise a confirmation error."""
        conn = _make_conn()
        plan = _plan_json(_drop_table_ops(), requires_confirmation=True)
        # dry_run=True is always safe — confirmation gate is bypassed.
        try:
            await apply(conn, plan, dry_run=True)
        except FerrumMigrationError as exc:
            pytest.fail(f"dry_run=True should not raise FerrumMigrationError: {exc}")

    @pytest.mark.asyncio
    async def test_drop_column_plan_without_confirm_raises(self) -> None:
        """MIG-2: DropColumn plan with confirm=False, dry_run=False raises FerrumMigrationError."""
        conn = _make_conn()
        plan = _plan_json(_drop_column_ops(), requires_confirmation=True)
        with pytest.raises(FerrumMigrationError):
            await apply(conn, plan, dry_run=False, confirm=False)

    # Gate-function layer: check_destructive_gate (token-based companion)
    def test_check_destructive_gate_without_token_raises(self) -> None:
        """MIG-2: check_destructive_gate without a token raises FerrumMigrationError."""
        with pytest.raises(FerrumMigrationError, match="confirmation token"):
            check_destructive_gate("some_plan_digest", confirmation_token=None)

    def test_check_destructive_gate_with_wrong_token_raises(self) -> None:
        """MIG-2: Wrong confirmation token (digest mismatch) is rejected."""
        digest = "plan_digest_abcdef"
        wrong_token = generate_token("completely_different_digest")
        with pytest.raises(FerrumMigrationError, match="does not match"):
            check_destructive_gate(digest, confirmation_token=wrong_token)

    def test_check_destructive_gate_with_correct_token_passes(self) -> None:
        """MIG-2: Correct confirmation token clears the destructive gate."""
        digest = "plan_digest_xyz12345"
        token = generate_token(digest)
        check_destructive_gate(digest, confirmation_token=token)  # must not raise


# ---------------------------------------------------------------------------
# MIG-3: non-dev env confirmation
# ---------------------------------------------------------------------------


class TestMIG3NonDevEnvConfirmation:
    @pytest.mark.asyncio
    async def test_non_dev_env_without_confirm_raises(self) -> None:
        """MIG-3: apply() with env='production' and confirm=False raises FerrumMigrationError."""
        conn = _make_conn()
        plan = _plan_json(_create_table_ops())
        with pytest.raises(FerrumMigrationError):
            await apply(conn, plan, env="production", confirm=False, dry_run=False)

    @pytest.mark.asyncio
    async def test_non_dev_env_with_confirm_does_not_raise_on_confirm_gate(
        self,
    ) -> None:
        """MIG-3: confirm=True clears the env gate; no FerrumMigrationError on the confirm gate."""
        conn = _make_conn()
        plan = _plan_json(_create_table_ops())
        try:
            await apply(conn, plan, env="production", confirm=True, dry_run=False)
        except FerrumMigrationError as exc:
            pytest.fail(f"confirm=True should pass the env gate, got FerrumMigrationError: {exc}")

    # Gate-function layer: check_environment_gate
    def test_check_env_gate_non_dev_without_match_raises(self) -> None:
        """MIG-3: check_environment_gate mismatch raises FerrumMigrationError."""
        with pytest.raises(FerrumMigrationError, match="production"):
            check_environment_gate("development", "production")

    def test_check_env_gate_staging_without_match_raises(self) -> None:
        """MIG-3: Staging target without matching declaration raises."""
        with pytest.raises(FerrumMigrationError, match="staging"):
            check_environment_gate("development", "staging")

    def test_check_env_gate_matching_declaration_passes(self) -> None:
        """MIG-3: Matching environment declaration clears the gate."""
        check_environment_gate("production", "production")  # must not raise

    @pytest.mark.asyncio
    async def test_unscoped_delete_requires_danger_api(self) -> None:
        """MIG-5 companion: unscoped QuerySet.delete() must raise FerrumDangerApiError."""
        from ferrum.models import Model
        from ferrum.queryset import QuerySet

        class T(Model):
            id: int = 0

        qs: QuerySet[T] = QuerySet(T)
        with pytest.raises(FerrumDangerApiError):
            await qs.delete()


# ---------------------------------------------------------------------------
# MIG-4: identifiers in generated SQL are double-quoted
# ---------------------------------------------------------------------------


class TestMIG4IdentifierQuoting:
    def test_create_table_identifiers_are_double_quoted(self) -> None:
        """MIG-4: CREATE TABLE emits double-quoted table and column identifiers."""
        sql = _op_to_sql(
            {
                "kind": "create_table",
                "table": "my_table",
                "columns": [
                    {
                        "name": "id",
                        "sql_type": "INTEGER",
                        "not_null": False,
                        "primary_key": True,
                    }
                ],
            }
        )
        assert '"my_table"' in sql, f"Expected double-quoted table name in: {sql!r}"
        assert '"id"' in sql, f"Expected double-quoted column name in: {sql!r}"

    def test_add_column_identifier_double_quoted(self) -> None:
        """MIG-4: ADD COLUMN emits double-quoted table and column identifiers.

        Note: _op_to_sql for add_column uses _col_def(op), so column fields
        (name, sql_type) sit at the top level of the op dict alongside table.
        """
        sql = _op_to_sql(
            {
                "kind": "add_column",
                "table": "users",
                "name": "email",
                "sql_type": "TEXT",
                "not_null": False,
            }
        )
        assert '"users"' in sql, f"Expected double-quoted table name in: {sql!r}"
        assert '"email"' in sql, f"Expected double-quoted column name in: {sql!r}"

    def test_drop_table_identifier_double_quoted(self) -> None:
        """MIG-4: DROP TABLE emits a double-quoted table identifier."""
        sql = _op_to_sql({"kind": "drop_table", "table": "orders"})
        assert '"orders"' in sql, f"Expected double-quoted table name in: {sql!r}"

    def test_rename_column_identifiers_double_quoted(self) -> None:
        """MIG-4: RENAME COLUMN emits double-quoted table, from, and to identifiers.

        Note: _op_to_sql uses 'from'/'to' keys (not 'old_name'/'new_name').
        """
        sql = _op_to_sql(
            {
                "kind": "rename_column",
                "table": "users",
                "from": "fname",
                "to": "first_name",
            }
        )
        assert '"users"' in sql, f"Expected double-quoted table name in: {sql!r}"
        assert '"fname"' in sql, f"Expected double-quoted old column name in: {sql!r}"
        assert '"first_name"' in sql, f"Expected double-quoted new column name in: {sql!r}"

    def test_drop_column_identifier_double_quoted(self) -> None:
        """MIG-4: DROP COLUMN emits double-quoted table and column identifiers."""
        sql = _op_to_sql({"kind": "drop_column", "table": "events", "column": "legacy_field"})
        assert '"events"' in sql, f"Expected double-quoted table name in: {sql!r}"
        assert '"legacy_field"' in sql, f"Expected double-quoted column name in: {sql!r}"


# ---------------------------------------------------------------------------
# MIG-5: no credential leak in dry-run output
# ---------------------------------------------------------------------------


class TestMIG5NoCredentialLeak:
    @pytest.mark.asyncio
    async def test_dry_run_output_does_not_contain_dsn(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """MIG-5: DSN password must not appear in any captured output during dry-run.

        A fake DSN is injected via env var. apply() in dry_run mode prints a plan
        summary; it must not echo the DSN or its password.
        """
        secret_password = "s3cr3t_P@ssw0rd_unique_marker"  # noqa: S105
        fake_dsn = f"postgresql://ferrum:{secret_password}@localhost:5432/testdb"
        conn = _make_conn()
        plan = _plan_json(_create_table_ops())

        with patch.dict(os.environ, {"FERRUM_DATABASE_URL": fake_dsn}):
            await apply(conn, plan, dry_run=True)

        captured = capsys.readouterr()
        combined = captured.out + captured.err
        assert secret_password not in combined, (
            f"DSN password leaked into dry-run output. Output: {combined[:500]!r}"
        )

    @pytest.mark.asyncio
    async def test_error_message_does_not_contain_dsn(self) -> None:
        """MIG-5: FerrumMigrationError raised by gate checks must not contain the DSN password."""
        secret_password = "s3cr3t_P@ssw0rd_gate_error"  # noqa: S105
        fake_dsn = f"postgresql://ferrum:{secret_password}@localhost:5432/testdb"
        conn = _make_conn()
        plan = _plan_json(_drop_table_ops(), requires_confirmation=True)

        with (
            patch.dict(os.environ, {"FERRUM_DATABASE_URL": fake_dsn}),
            pytest.raises(FerrumMigrationError) as exc_info,
        ):
            await apply(conn, plan, dry_run=False, confirm=False)

        assert secret_password not in str(exc_info.value), (
            f"DSN password leaked into gate error message: {exc_info.value!r}"
        )


# ---------------------------------------------------------------------------
# MIG-6: Token binding and replay prevention
# ---------------------------------------------------------------------------


class TestMIG6TokenReplay:
    def test_token_binds_to_specific_digest(self) -> None:
        """MIG-6/MIG-8: A token for digest A must not validate against digest B."""
        digest_a = "digest_a_x" * 3
        digest_b = "digest_b_y" * 3
        token_a = generate_token(digest_a)
        assert validate_token(token_a, digest_a)
        assert not validate_token(token_a, digest_b)

    @pytest.mark.asyncio
    async def test_token_replay_after_apply_rejected(self) -> None:
        """MIG-6: Re-using a confirmation token after a successful apply is rejected.

        The ledger replay guard must fire before any second mutation attempt.
        """
        pool = AsyncMock()
        pool.execute = AsyncMock(return_value=None)
        conn = _make_conn()
        conn._require_pool.return_value = pool
        plan = _plan_json(_create_table_ops())
        token = _confirmation_token(plan)

        with (
            patch(
                "ferrum.migrations.orchestrator.is_applied",
                new_callable=AsyncMock,
            ) as mock_is_applied,
            patch(
                "ferrum.migrations.orchestrator.record_applied",
                new_callable=AsyncMock,
            ) as mock_record,
        ):
            mock_is_applied.return_value = False
            result = await apply(conn, plan, dry_run=False, confirm=True, token=token)
            assert result.applied is True
            mock_record.assert_awaited_once()

            mock_is_applied.return_value = True
            with pytest.raises(FerrumMigrationError, match="already been applied"):
                await apply(conn, plan, dry_run=False, confirm=True, token=token)

        pool.execute.assert_awaited_once()


# ---------------------------------------------------------------------------
# MIG-7: Confirmation token not stored in plan / public structures
# ---------------------------------------------------------------------------


class TestMIG7TokenNotInPublicStructures:
    def test_token_not_in_plan_object(self) -> None:
        """MIG-7: Confirmation tokens are not stored in the MigrationPlan object."""
        plan = MigrationPlan(dry_run_completed=True)
        assert not hasattr(plan, "token")
        assert not hasattr(plan, "confirmation_token")

    def test_token_not_emitted_to_stdout_in_dry_run_output(
        self,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """MIG-7: Dry-run output must not contain a confirmation token value."""
        from ferrum.cli.migrations_cmd import migrations_dry_run

        plan = _plan_json(_create_table_ops())
        plan_path = tmp_path / "plan.json"
        plan_path.write_text(plan, encoding="utf-8")
        secret_token = _confirmation_token(plan)
        monkeypatch.setenv("FERRUM_MIGRATION_TOKEN", secret_token)

        migrations_dry_run(plan_file=plan_path, environment="development")

        captured = capsys.readouterr()
        combined = captured.out + captured.err
        assert secret_token not in combined, (
            f"Confirmation token leaked into dry-run output: {combined[:500]!r}"
        )


# ---------------------------------------------------------------------------
# MIG-8: Token injection path and malformed-token rejection
# ---------------------------------------------------------------------------


class TestMIG8TokenInjectionPath:
    def test_token_malformed_rejected(self) -> None:
        """MIG-8: Malformed token string is rejected without raising an exception."""
        assert not validate_token("no-dot-separator", "any_digest")
        assert not validate_token("", "any_digest")

    def test_token_accepted_via_env_var(
        self,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """MIG-8: CLI apply accepts token via FERRUM_MIGRATION_TOKEN env variable."""
        from ferrum.cli.migrations_cmd import migrations_apply

        plan = _plan_json(_create_table_ops())
        plan_path = tmp_path / "plan.json"
        plan_path.write_text(plan, encoding="utf-8")
        token = _confirmation_token(plan)
        monkeypatch.setenv("FERRUM_MIGRATION_TOKEN", token)
        monkeypatch.setenv("FERRUM_DATABASE_URL", "postgresql://ferrum:changeme@127.0.0.1/db")

        pool = AsyncMock()
        pool.execute = AsyncMock(return_value=None)
        mock_conn = MagicMock()
        mock_conn._require_pool.return_value = pool
        mock_cm = MagicMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_cm.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("ferrum.connection.connect", return_value=mock_cm),
            patch(
                "ferrum.migrations.orchestrator.is_applied",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "ferrum.migrations.orchestrator.record_applied",
                new_callable=AsyncMock,
            ),
        ):
            migrations_apply(plan_file=plan_path, confirm=False, dry_run=False)

        pool.execute.assert_awaited()


# ---------------------------------------------------------------------------
# MIG-2 gate: destructive ops with confirm=False raise FerrumMigrationError
# (security gate — not a duplicate of unit tests; this is the release-qualification
# assertion that the gate is structurally enforced before any SQL reaches the DB)
# ---------------------------------------------------------------------------


class TestDestructiveGateOnApply:
    @pytest.mark.asyncio
    async def test_drop_table_without_confirm_raises_before_sql(self) -> None:
        """MIG-2 security gate: drop_table without confirm=True must be blocked.

        The gate must fire before pool.execute is called — no partial mutation.
        """
        conn = _make_conn()
        plan = _plan_json(_drop_table_ops(), requires_confirmation=False)

        with pytest.raises(FerrumMigrationError):
            await apply(conn, plan, dry_run=False, confirm=False)

        conn._require_pool.return_value.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_drop_column_without_confirm_raises_before_sql(self) -> None:
        """MIG-2 security gate: drop_column without confirm=True must be blocked."""
        conn = _make_conn()
        plan = _plan_json(_drop_column_ops(), requires_confirmation=False)

        with pytest.raises(FerrumMigrationError):
            await apply(conn, plan, dry_run=False, confirm=False)

        conn._require_pool.return_value.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_raw_sql_without_confirm_raises_before_sql(self) -> None:
        """MIG-2 security gate: raw_sql op without confirm=True must be blocked.

        raw_sql is treated as destructive (AGENTS.md §3) regardless of the
        ``safe`` flag in the op dict.
        """
        conn = _make_conn()
        plan = _plan_json(
            [{"kind": "raw_sql", "sql": "DROP SCHEMA public CASCADE", "safe": False}],
            requires_confirmation=False,
        )

        with pytest.raises(FerrumMigrationError):
            await apply(conn, plan, dry_run=False, confirm=False)

        conn._require_pool.return_value.execute.assert_not_called()


# ---------------------------------------------------------------------------
# compute_digest: output must be opaque hex — content values never appear
# ---------------------------------------------------------------------------


class TestComputeDigestOpacity:
    def test_digest_is_pure_hex_string(self) -> None:
        """compute_digest must return a 64-char lowercase hex string."""
        import re

        from ferrum.migrations.ledger import compute_digest

        digest = compute_digest("0001_create_note", "class Migration: pass")
        assert re.fullmatch(r"[0-9a-f]{64}", digest), (
            f"compute_digest must return lowercase 64-char hex, got: {digest!r}"
        )

    def test_digest_does_not_contain_raw_content(self) -> None:
        """compute_digest output must not contain any literal from the input content (CRED-1)."""
        import re

        from ferrum.migrations.ledger import compute_digest

        sensitive_value = "password=s3cr3t_migration_marker"
        digest = compute_digest("0001_sensitive", sensitive_value)

        # The digest must be pure hex — raw content strings must never appear.
        assert sensitive_value not in digest
        assert "s3cr3t_migration_marker" not in digest
        assert re.fullmatch(r"[0-9a-f]{64}", digest), f"digest must be opaque hex, got: {digest!r}"

    def test_digest_does_not_contain_migration_name(self) -> None:
        """The literal migration name must not appear verbatim in the digest output."""
        from ferrum.migrations.ledger import compute_digest

        name = "0001_create_unique_sentinel"
        digest = compute_digest(name, "content")
        assert name not in digest
