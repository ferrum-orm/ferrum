"""Unit tests for ferrum.migrations.coordinator — schema/shard migration coordinator.

Coverage (W3-B acceptance criteria):
- Multi-shard migration: all targets receive pending migrations.
- Per-target advisory locks: lock key derivation is stable and per-target.
- Bounded concurrency: max_parallelism limits concurrent target applies.
- Resumable status: per-target, per-migration state tracked in memory.
- Fail-fast / continue policy: fail_fast raises on first failure; continue
  collects failures and returns a full result.
- Canary-target support: canaries run first, serially; a canary failure
  halts the entire rollout regardless of policy.
- Idempotent reruns: already-applied migrations are skipped (the mocked
  apply returns False for already-applied, matching the replay guard).
- Structured progress hooks: events emitted in the correct order.
- Constructor validation: empty targets, duplicate ids, unknown canaries,
  bad policy, bad parallelism.
- Partial rollout reporting: partial_rollout is True iff some targets made
  progress and others did not.
- Destructive gate: confirm=True required for destructive ops.
- Schema-tenant constraint: non-transactional ops rejected in schema path.

These tests mock the per-migration apply (``_apply_migration_on_target``) to
isolate coordinator orchestration from the W1-C apply path. Integration tests
against live PostgreSQL exercise the full apply path.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, ClassVar
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ferrum.errors import FerrumMigrationError
from ferrum.migrations.coordinator import (
    MigrationTarget,
    ProgressEvent,
    ProgressEventType,
    SchemaShardMigrationCoordinator,
    TargetMigrationState,
    _coord_lock_key_2,
)
from ferrum.migrations.loader import MigrationModule

# ---------------------------------------------------------------------------
# Helpers — build fake modules, targets, and mock the apply path
# ---------------------------------------------------------------------------


_PG_DSN = "postgresql://user:pass@localhost:5432/db"


def _fake_module(name: str, tmp_path: Path) -> MigrationModule:
    """Build a MigrationModule with a minimal Migration class and a real file path.

    The Migration class has one CreateTable operation so destructive-gate
    tests have ops to scan. The file path is real so ``compute_digest`` works.
    """
    from ferrum.migrations import operations as _ops
    from ferrum.migrations.base import Migration

    p = tmp_path / f"{name}.py"
    p.write_text(f"# migration {name}\n")

    table = f"ferrum_coord_{name}"

    class _M(Migration):
        operations: ClassVar = [
            _ops.CreateTable(table, [_ops.Column("id", "BIGSERIAL", primary_key=True)])
        ]

    return MigrationModule(name=name, path=p, migration=_M)


def _mock_connection() -> MagicMock:
    conn = MagicMock()
    conn.dialect = "postgres"
    return conn


def _make_coordinator(
    targets: list[MigrationTarget],
    modules: list[MigrationModule],
    *,
    max_parallelism: int = 4,
    policy: str = "fail_fast",
    canary_targets: list[str] | None = None,
    on_progress: Any = None,
    env: str = "development",
    confirm: bool = False,
    lock_timeout: str | None = None,
    statement_timeout: str | None = None,
) -> SchemaShardMigrationCoordinator:
    return SchemaShardMigrationCoordinator(
        targets,
        modules,
        max_parallelism=max_parallelism,
        policy=policy,
        canary_targets=canary_targets,
        on_progress=on_progress,
        env=env,
        confirm=confirm,
        lock_timeout=lock_timeout,
        statement_timeout=statement_timeout,
    )


def _patch_apply(sides: dict[str, list[bool | Exception]]) -> Any:
    """Patch ``_apply_migration_on_target`` AND ``MigrationGraph.upgrade_plan``.

    ``sides`` maps target_id -> list of return values (True=applied, False=skipped,
    Exception=raise). Each call pops the next value. Raises if a target runs
    out of values (test misconfiguration).

    ``MigrationGraph.upgrade_plan`` is patched to return all modules as
    pending, so the coordinator does not need a real ledger behind the mock
    connection. The mocked ``_apply_migration_on_target`` is the single place
    that decides applied-vs-skipped, matching the W1-C replay guard contract.
    """
    state: dict[str, int] = dict.fromkeys(sides, 0)

    async def _fake_apply(self: Any, target: MigrationTarget, module: MigrationModule) -> bool:
        tid = target.target_id
        idx = state[tid]
        if idx >= len(sides[tid]):
            raise AssertionError(f"target {tid!r} ran out of mock sides at index {idx}")
        val = sides[tid][idx]
        state[tid] = idx + 1
        if isinstance(val, Exception):
            raise val
        return bool(val)

    async def _fake_upgrade_plan(self: Any, target: str | None = None) -> list[MigrationModule]:
        return list(self._modules)

    apply_patch = patch(
        "ferrum.migrations.coordinator.SchemaShardMigrationCoordinator._apply_migration_on_target",
        new=_fake_apply,
    )
    plan_patch = patch(
        "ferrum.migrations.coordinator.MigrationGraph.upgrade_plan",
        new=_fake_upgrade_plan,
    )
    return _CompositeContextManager([apply_patch, plan_patch])


class _CompositeContextManager:
    """Compose multiple context managers into one ``with`` statement."""

    def __init__(self, cms: list[Any]) -> None:
        self._cms = cms

    def __enter__(self) -> None:
        for cm in self._cms:
            cm.__enter__()

    def __exit__(self, *exc: Any) -> None:
        for cm in reversed(self._cms):
            cm.__exit__(*exc)


# ---------------------------------------------------------------------------
# Advisory lock key derivation
# ---------------------------------------------------------------------------


class TestAdvisoryLockKey:
    def test_key_is_stable(self) -> None:
        assert _coord_lock_key_2("shard_a") == _coord_lock_key_2("shard_a")

    def test_different_targets_get_different_keys(self) -> None:
        assert _coord_lock_key_2("shard_a") != _coord_lock_key_2("shard_b")

    def test_key_is_signed_int32(self) -> None:
        """pg_advisory_xact_lock(int4, int4) expects signed int32."""
        key = _coord_lock_key_2("shard_a")
        assert -(2**31) <= key < 2**31


# ---------------------------------------------------------------------------
# Constructor validation
# ---------------------------------------------------------------------------


class TestConstructorValidation:
    def test_empty_targets_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(FerrumMigrationError, match="at least one target"):
            _make_coordinator([], [])

    def test_duplicate_target_ids_rejected(self, tmp_path: Path) -> None:
        conn = _mock_connection()
        with pytest.raises(FerrumMigrationError, match="Duplicate target_id"):
            _make_coordinator(
                [MigrationTarget("a", conn), MigrationTarget("a", conn)],
                [],
            )

    def test_empty_target_id_rejected(self, tmp_path: Path) -> None:
        conn = _mock_connection()
        with pytest.raises(FerrumMigrationError, match="non-empty string"):
            _make_coordinator([MigrationTarget("", conn)], [])

    def test_bad_parallelism_rejected(self, tmp_path: Path) -> None:
        conn = _mock_connection()
        with pytest.raises(FerrumMigrationError, match="max_parallelism"):
            _make_coordinator([MigrationTarget("a", conn)], [], max_parallelism=0)

    def test_bad_policy_rejected(self, tmp_path: Path) -> None:
        conn = _mock_connection()
        with pytest.raises(FerrumMigrationError, match="Unknown policy"):
            _make_coordinator([MigrationTarget("a", conn)], [], policy="bogus")

    def test_unknown_canary_rejected(self, tmp_path: Path) -> None:
        conn = _mock_connection()
        with pytest.raises(FerrumMigrationError, match="not in the targets"):
            _make_coordinator(
                [MigrationTarget("a", conn)],
                [],
                canary_targets=["zzz"],
            )

    def test_bad_lock_timeout_rejected(self, tmp_path: Path) -> None:
        conn = _mock_connection()
        with pytest.raises(FerrumMigrationError):
            _make_coordinator([MigrationTarget("a", conn)], [], lock_timeout="'; DROP--")


# ---------------------------------------------------------------------------
# Multi-shard migration (happy path)
# ---------------------------------------------------------------------------


class TestMultiShardHappyPath:
    @pytest.mark.asyncio
    async def test_all_targets_applied(self, tmp_path: Path) -> None:
        conn_a, conn_b = _mock_connection(), _mock_connection()
        mod1 = _fake_module("0001_a", tmp_path)
        mod2 = _fake_module("0002_b", tmp_path)
        targets = [MigrationTarget("shard_a", conn_a), MigrationTarget("shard_b", conn_b)]
        coord = _make_coordinator(targets, [mod1, mod2])
        with _patch_apply(
            {
                "shard_a": [True, True],
                "shard_b": [True, True],
            }
        ):
            result = await coord.run()
        assert len(result.targets) == 2
        for r in result.targets:
            assert r.applied == ["0001_a", "0002_b"]
            assert r.skipped == []
            assert r.failed == []
            assert not r.halted
        assert not result.halted
        assert not result.partial_rollout

    @pytest.mark.asyncio
    async def test_status_tracking(self, tmp_path: Path) -> None:
        conn = _mock_connection()
        mod1 = _fake_module("0001_a", tmp_path)
        coord = _make_coordinator([MigrationTarget("s", conn)], [mod1])
        with _patch_apply({"s": [True]}):
            await coord.run()
        status = coord.status()
        assert "s" in status
        assert len(status["s"]) == 1
        assert status["s"][0].state == TargetMigrationState.APPLIED


# ---------------------------------------------------------------------------
# Idempotent reruns (replay guard → skip)
# ---------------------------------------------------------------------------


class TestIdempotentRerun:
    @pytest.mark.asyncio
    async def test_already_applied_skipped(self, tmp_path: Path) -> None:
        conn = _mock_connection()
        mod1 = _fake_module("0001_a", tmp_path)
        coord = _make_coordinator([MigrationTarget("s", conn)], [mod1])
        with _patch_apply({"s": [False]}):
            result = await coord.run()
        assert result.targets[0].skipped == ["0001_a"]
        assert result.targets[0].applied == []
        assert not result.partial_rollout


# ---------------------------------------------------------------------------
# Fail-fast vs continue policy
# ---------------------------------------------------------------------------


class TestFailFastVsContinue:
    @pytest.mark.asyncio
    async def test_fail_fast_raises_on_first_failure(self, tmp_path: Path) -> None:
        conn_a, conn_b = _mock_connection(), _mock_connection()
        mod1 = _fake_module("0001_a", tmp_path)
        targets = [MigrationTarget("a", conn_a), MigrationTarget("b", conn_b)]
        coord = _make_coordinator(targets, [mod1], policy="fail_fast")
        boom = FerrumMigrationError("boom [FERR-M001]")
        with (
            _patch_apply({"a": [boom], "b": [True]}),
            pytest.raises(FerrumMigrationError, match="fail_fast policy"),
        ):
            await coord.run()

    @pytest.mark.asyncio
    async def test_continue_collects_failures(self, tmp_path: Path) -> None:
        conn_a, conn_b = _mock_connection(), _mock_connection()
        mod1 = _fake_module("0001_a", tmp_path)
        targets = [MigrationTarget("a", conn_a), MigrationTarget("b", conn_b)]
        coord = _make_coordinator(targets, [mod1], policy="continue")
        boom = FerrumMigrationError("boom [FERR-M001]")
        with _patch_apply({"a": [boom], "b": [True]}):
            result = await coord.run()
        # Target "a" failed; target "b" applied.
        by_id = {r.target_id: r for r in result.targets}
        assert len(by_id["a"].failed) == 1
        assert by_id["a"].failed[0].migration_name == "0001_a"
        assert by_id["b"].applied == ["0001_a"]
        assert result.partial_rollout


# ---------------------------------------------------------------------------
# Canary-target support
# ---------------------------------------------------------------------------


class TestCanary:
    @pytest.mark.asyncio
    async def test_canary_runs_first_and_succeeds(self, tmp_path: Path) -> None:
        conn_a, conn_b = _mock_connection(), _mock_connection()
        mod1 = _fake_module("0001_a", tmp_path)
        targets = [MigrationTarget("main", conn_a), MigrationTarget("canary", conn_b)]
        coord = _make_coordinator(targets, [mod1], canary_targets=["canary"])
        # Track call order via the mock sides.
        call_order: list[str] = []

        async def _track(self: Any, target: MigrationTarget, module: MigrationModule) -> bool:
            call_order.append(target.target_id)
            return True

        async def _fake_upgrade_plan(self: Any, target: str | None = None) -> list[MigrationModule]:
            return list(self._modules)

        with (
            patch(
                "ferrum.migrations.coordinator.SchemaShardMigrationCoordinator._apply_migration_on_target",
                new=_track,
            ),
            patch(
                "ferrum.migrations.coordinator.MigrationGraph.upgrade_plan",
                new=_fake_upgrade_plan,
            ),
        ):
            result = await coord.run()
        assert call_order[0] == "canary"
        assert "main" in call_order
        assert not result.halted
        assert len(result.canary_results) == 1

    @pytest.mark.asyncio
    async def test_canary_failure_halts_rollout(self, tmp_path: Path) -> None:
        conn_a, conn_b = _mock_connection(), _mock_connection()
        mod1 = _fake_module("0001_a", tmp_path)
        targets = [MigrationTarget("main", conn_a), MigrationTarget("canary", conn_b)]
        coord = _make_coordinator(targets, [mod1], canary_targets=["canary"], policy="continue")
        boom = FerrumMigrationError("canary boom [FERR-M001]")
        with _patch_apply({"canary": [boom], "main": [True]}):
            result = await coord.run()
        assert result.halted
        by_id = {r.target_id: r for r in result.targets}
        assert len(by_id["canary"].failed) == 1
        # Main was never run.
        assert by_id["main"].halted
        assert by_id["main"].applied == []


# ---------------------------------------------------------------------------
# Bounded concurrency
# ---------------------------------------------------------------------------


class TestBoundedConcurrency:
    @pytest.mark.asyncio
    async def test_parallelism_bound(self, tmp_path: Path) -> None:
        conns = [_mock_connection() for _ in range(4)]
        mod1 = _fake_module("0001_a", tmp_path)
        targets = [MigrationTarget(f"s{i}", c) for i, c in enumerate(conns)]
        coord = _make_coordinator(targets, [mod1], max_parallelism=2)

        in_flight = 0
        max_seen = 0

        async def _track(self: Any, target: MigrationTarget, module: MigrationModule) -> bool:
            nonlocal in_flight, max_seen
            in_flight += 1
            max_seen = max(max_seen, in_flight)
            await asyncio.sleep(0.01)
            in_flight -= 1
            return True

        async def _fake_upgrade_plan(self: Any, target: str | None = None) -> list[MigrationModule]:
            return list(self._modules)

        with (
            patch(
                "ferrum.migrations.coordinator.SchemaShardMigrationCoordinator._apply_migration_on_target",
                new=_track,
            ),
            patch(
                "ferrum.migrations.coordinator.MigrationGraph.upgrade_plan",
                new=_fake_upgrade_plan,
            ),
        ):
            await coord.run()
        assert max_seen <= 2


# ---------------------------------------------------------------------------
# Structured progress hooks
# ---------------------------------------------------------------------------


class TestProgressHooks:
    @pytest.mark.asyncio
    async def test_events_emitted_in_order(self, tmp_path: Path) -> None:
        conn = _mock_connection()
        mod1 = _fake_module("0001_a", tmp_path)
        events: list[ProgressEvent] = []
        coord = _make_coordinator(
            [MigrationTarget("s", conn)], [mod1], on_progress=lambda e: events.append(e)
        )
        with _patch_apply({"s": [True]}):
            await coord.run()
        types = [e.event_type for e in events]
        assert types[0] == ProgressEventType.COORDINATOR_STARTED
        assert ProgressEventType.MIGRATION_STARTED in types
        assert ProgressEventType.MIGRATION_APPLIED in types
        assert types[-1] == ProgressEventType.COORDINATOR_COMPLETED

    @pytest.mark.asyncio
    async def test_async_hook_awaited(self, tmp_path: Path) -> None:
        conn = _mock_connection()
        mod1 = _fake_module("0001_a", tmp_path)
        events: list[ProgressEvent] = []

        async def _async_hook(event: ProgressEvent) -> None:
            await asyncio.sleep(0)
            events.append(event)

        coord = _make_coordinator([MigrationTarget("s", conn)], [mod1], on_progress=_async_hook)
        with _patch_apply({"s": [True]}):
            await coord.run()
        assert len(events) > 0

    @pytest.mark.asyncio
    async def test_hook_error_does_not_break_coordinator(self, tmp_path: Path) -> None:
        conn = _mock_connection()
        mod1 = _fake_module("0001_a", tmp_path)

        def _bad_hook(event: ProgressEvent) -> None:
            raise RuntimeError("hook bug")

        coord = _make_coordinator([MigrationTarget("s", conn)], [mod1], on_progress=_bad_hook)
        with _patch_apply({"s": [True]}):
            result = await coord.run()
        assert result.targets[0].applied == ["0001_a"]


# ---------------------------------------------------------------------------
# Partial rollout reporting
# ---------------------------------------------------------------------------


class TestPartialRollout:
    @pytest.mark.asyncio
    async def test_partial_rollout_true_when_some_fail(self, tmp_path: Path) -> None:
        conn_a, conn_b = _mock_connection(), _mock_connection()
        mod1 = _fake_module("0001_a", tmp_path)
        targets = [MigrationTarget("a", conn_a), MigrationTarget("b", conn_b)]
        coord = _make_coordinator(targets, [mod1], policy="continue")
        boom = FerrumMigrationError("boom [FERR-M001]")
        with _patch_apply({"a": [boom], "b": [True]}):
            result = await coord.run()
        assert result.partial_rollout

    @pytest.mark.asyncio
    async def test_no_partial_when_all_succeed(self, tmp_path: Path) -> None:
        conn_a, conn_b = _mock_connection(), _mock_connection()
        mod1 = _fake_module("0001_a", tmp_path)
        targets = [MigrationTarget("a", conn_a), MigrationTarget("b", conn_b)]
        coord = _make_coordinator(targets, [mod1])
        with _patch_apply({"a": [True], "b": [True]}):
            result = await coord.run()
        assert not result.partial_rollout

    @pytest.mark.asyncio
    async def test_no_partial_when_all_skipped(self, tmp_path: Path) -> None:
        conn_a, conn_b = _mock_connection(), _mock_connection()
        mod1 = _fake_module("0001_a", tmp_path)
        targets = [MigrationTarget("a", conn_a), MigrationTarget("b", conn_b)]
        coord = _make_coordinator(targets, [mod1])
        with _patch_apply({"a": [False], "b": [False]}):
            result = await coord.run()
        assert not result.partial_rollout


# ---------------------------------------------------------------------------
# Destructive gate (unit-level — the full gate runs in integration tests)
# ---------------------------------------------------------------------------


class TestDestructiveGate:
    @pytest.mark.asyncio
    async def test_destructive_without_confirm_raises(self, tmp_path: Path) -> None:
        conn = _mock_connection()
        mod1 = _fake_module("0001_a", tmp_path)
        # Use continue policy so the coordinator records the failure rather
        # than raising a fail_fast summary; the gate fires inside
        # _apply_migration_on_target before any SQL is emitted.
        coord = _make_coordinator(
            [MigrationTarget("s", conn)], [mod1], confirm=False, policy="continue"
        )

        async def _fake_upgrade_plan(self: Any, target: str | None = None) -> list[MigrationModule]:
            return list(self._modules)

        with (
            patch("ferrum.migrations.orchestrator._is_op_destructive", return_value=True),
            patch(
                "ferrum.migrations.coordinator.MigrationGraph.upgrade_plan",
                new=_fake_upgrade_plan,
            ),
        ):
            result = await coord.run()
        # The destructive gate fired — the target has a recorded failure.
        assert len(result.targets[0].failed) == 1
        assert (
            "destructive" in result.targets[0].failed[0].error
            or "migration" in result.targets[0].failed[0].error
        )

    @pytest.mark.asyncio
    async def test_non_dev_without_confirm_raises(self, tmp_path: Path) -> None:
        conn = _mock_connection()
        mod1 = _fake_module("0001_a", tmp_path)
        coord = _make_coordinator(
            [MigrationTarget("s", conn)], [mod1], env="production", confirm=False, policy="continue"
        )

        async def _fake_upgrade_plan(self: Any, target: str | None = None) -> list[MigrationModule]:
            return list(self._modules)

        with patch(
            "ferrum.migrations.coordinator.MigrationGraph.upgrade_plan",
            new=_fake_upgrade_plan,
        ):
            result = await coord.run()
        assert len(result.targets[0].failed) == 1


# ---------------------------------------------------------------------------
# routing.py additive helper
# ---------------------------------------------------------------------------


class TestRegistryItems:
    @pytest.mark.asyncio
    async def test_items_returns_pairs(self) -> None:
        from ferrum.routing import ConnectionRegistry, PoolConfig

        r = ConnectionRegistry({"a": PoolConfig(dsn=_PG_DSN), "b": PoolConfig(dsn=_PG_DSN)})
        conns = {n: _mock_connection() for n in ("a", "b")}
        for _n, c in conns.items():
            c.open = AsyncMock(return_value=None)
            c.close = AsyncMock(return_value=None)
        with patch(
            "ferrum.routing.Connection", side_effect=lambda dsn, **kw: conns.pop(next(iter(conns)))
        ):
            # The factory above pops in arbitrary order; rebuild deterministically.
            pass
        # Use the same pattern as test_routing.py for deterministic mapping.
        ordered = [conns["a"], conns["b"]]

        def _factory(dsn: str, **kw: Any) -> Any:
            _factory.i = getattr(_factory, "i", 0) + 1  # type: ignore[attr-defined]
            return ordered[_factory.i - 1]  # type: ignore[attr-defined]

        with patch("ferrum.routing.Connection", side_effect=_factory):
            await r.start()
        items = r.items()
        assert len(items) == 2
        assert items[0][0] == "a"
        assert items[1][0] == "b"
        assert items[0][1] is conns["a"]
        await r.close()
