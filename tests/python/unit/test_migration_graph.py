"""Unit tests for the migration graph, reversibility, data migrations, and offline SQL.

W3-A coverage:
- ``loader.topological_sort`` and ``loader.detect_cycle`` (cycle detection,
  deterministic ordering, missing-dependency error).
- ``base.is_reversible`` / ``base.reverse_classifications`` (reversibility
  contract).
- ``orchestrator.MigrationGraph`` (topological order, target upgrade/
  downgrade, status, recovery guidance, dependency lookup, unknown-name
  error).
- ``orchestrator.DataMigration`` / ``run_data_migration`` (transaction
  policies, untrusted-source rejection, callable failure propagation).
- ``orchestrator.generate_offline_sql`` (per-migration checksums, phase
  annotations, destructive flag, reversibility flag, topological order).

No live database is required for this file; the integration tests in
``tests/python/integration/test_migration_graph.py`` exercise the ledger
queries and the data-migration runner against PostgreSQL.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import ClassVar
from unittest.mock import AsyncMock, MagicMock

import pytest

from ferrum.errors import FerrumMigrationError
from ferrum.migrations import operations
from ferrum.migrations.base import Migration, is_reversible, reverse_classifications
from ferrum.migrations.loader import (
    MigrationModule,
    detect_cycle,
    scan,
    topological_sort,
)
from ferrum.migrations.orchestrator import (
    DataMigration,
    MigrationGraph,
    OfflineSqlPlan,
    generate_offline_sql,
    run_data_migration,
)

# ---------------------------------------------------------------------------
# Helpers — build MigrationModule-like objects without touching disk for the
# pure graph tests, and write real files when ``module.path`` must be readable.
# ---------------------------------------------------------------------------


def _migration_class(
    *, deps: list[str] | None = None, reverse: list | None = None
) -> type[Migration]:
    """Build a Migration subclass with the given dependencies / reverse_operations.

    Uses ``type()`` so the class-body mutable-default lint (RUF012) does not
    fire on intentional test fixtures.
    """
    namespace: dict[str, object] = {
        "dependencies": list(deps or []),
        "operations": [],
        "reverse_operations": list(reverse or []),
    }
    return type("_M", (Migration,), namespace)


def _module(
    name: str, *, deps: list[str] | None = None, path: Path | None = None
) -> MigrationModule:
    """Build a MigrationModule in memory (path optional for pure graph tests)."""
    # Tests never write to this path; it is only a placeholder for in-memory
    # graph queries that do not read file content.
    placeholder = path or Path(f"/tmp/{name}.py")  # noqa: S108
    return MigrationModule(name=name, path=placeholder, migration=_migration_class(deps=deps))


def _module_with_ops(
    name: str,
    *,
    deps: list[str] | None = None,
    forward: list | None = None,
    reverse: list | None = None,
    path: Path | None = None,
) -> MigrationModule:
    """Build a MigrationModule whose Migration class carries real operations."""
    namespace: dict[str, object] = {
        "dependencies": list(deps or []),
        "operations": list(forward or []),
        "reverse_operations": list(reverse or []),
    }
    cls = type("_M", (Migration,), namespace)
    placeholder = path or Path(f"/tmp/{name}.py")  # noqa: S108
    return MigrationModule(name=name, path=placeholder, migration=cls)


_MIGRATION_FILE_TEMPLATE = """\
from ferrum.migrations import Migration
from ferrum.migrations import operations


class Migration(Migration):
    dependencies = {deps!r}
    operations = []
"""


def _write_migration_file(dir_path: Path, filename: str, *, deps: list[str] | None = None) -> Path:
    p = dir_path / filename
    p.write_text(_MIGRATION_FILE_TEMPLATE.format(deps=deps or []))
    return p


# ---------------------------------------------------------------------------
# loader.topological_sort
# ---------------------------------------------------------------------------


class TestTopologicalSort:
    def test_single_module(self) -> None:
        result = topological_sort([_module("0001_a")])
        assert [m.name for m in result] == ["0001_a"]

    def test_chain_in_order(self) -> None:
        mods = [
            _module("0002_b", deps=["0001_a"]),
            _module("0001_a"),
            _module("0003_c", deps=["0002_b"]),
        ]
        result = topological_sort(mods)
        assert [m.name for m in result] == ["0001_a", "0002_b", "0003_c"]

    def test_independent_modules_sorted_for_determinism(self) -> None:
        mods = [
            _module("0002_b"),
            _module("0001_a"),
        ]
        result = topological_sort(mods)
        # Same dependency level → name-sorted for deterministic output.
        assert [m.name for m in result] == ["0001_a", "0002_b"]

    def test_diamond_dependency(self) -> None:
        mods = [
            _module("0001_a"),
            _module("0002_b", deps=["0001_a"]),
            _module("0003_c", deps=["0001_a"]),
            _module("0004_d", deps=["0002_b", "0003_c"]),
        ]
        result = topological_sort(mods)
        names = [m.name for m in result]
        assert names[0] == "0001_a"
        assert names[-1] == "0004_d"
        assert set(names[:3]) == {"0001_a", "0002_b", "0003_c"}
        # 0002_b must precede 0004_d.
        assert names.index("0002_b") < names.index("0004_d")
        assert names.index("0003_c") < names.index("0004_d")

    def test_missing_dependency_raises_value_error(self) -> None:
        mods = [_module("0001_a", deps=["0099_ghost"])]
        with pytest.raises(ValueError, match="0099_ghost"):
            topological_sort(mods)

    def test_cycle_raises_value_error(self) -> None:
        mods = [
            _module("0001_a", deps=["0002_b"]),
            _module("0002_b", deps=["0001_a"]),
        ]
        with pytest.raises(ValueError, match=r"[Cc]ycle"):
            topological_sort(mods)


# ---------------------------------------------------------------------------
# loader.detect_cycle
# ---------------------------------------------------------------------------


class TestDetectCycle:
    def test_acyclic_returns_none(self) -> None:
        mods = [
            _module("0001_a"),
            _module("0002_b", deps=["0001_a"]),
        ]
        assert detect_cycle(mods) is None

    def test_two_node_cycle_returns_names(self) -> None:
        mods = [
            _module("0001_a", deps=["0002_b"]),
            _module("0002_b", deps=["0001_a"]),
        ]
        cycled = detect_cycle(mods)
        assert cycled is not None
        assert set(cycled) == {"0001_a", "0002_b"}

    def test_three_node_cycle_returns_all_three(self) -> None:
        mods = [
            _module("0001_x", deps=["0003_z"]),
            _module("0002_y", deps=["0001_x"]),
            _module("0003_z", deps=["0002_y"]),
        ]
        cycled = detect_cycle(mods)
        assert cycled is not None
        assert set(cycled) == {"0001_x", "0002_y", "0003_z"}

    def test_missing_dependency_returns_none(self) -> None:
        # Missing dep is a distinct failure mode (topological_sort raises);
        # detect_cycle stays single-purpose and returns None.
        mods = [_module("0001_a", deps=["0099_ghost"])]
        assert detect_cycle(mods) is None


# ---------------------------------------------------------------------------
# base.is_reversible / reverse_classifications
# ---------------------------------------------------------------------------


class TestReversibilityHelpers:
    def test_default_migration_is_irreversible(self) -> None:
        assert is_reversible(Migration) is False

    def test_empty_reverse_operations_is_irreversible(self) -> None:
        M = _migration_class(reverse=[])
        assert is_reversible(M) is False

    def test_non_empty_reverse_operations_is_reversible(self) -> None:
        M = _migration_class(reverse=[operations.DropTable("t")])
        assert is_reversible(M) is True

    def test_reverse_classifications_empty_for_irreversible(self) -> None:
        M = _migration_class()
        assert reverse_classifications(M) == []

    def test_reverse_classifications_preserves_order(self) -> None:
        M = _migration_class(
            reverse=[
                operations.DropColumn("t", "c"),  # destructive
                operations.RenameColumn("t", "a", "b"),  # safe
            ]
        )
        assert reverse_classifications(M) == ["destructive", "safe"]


# ---------------------------------------------------------------------------
# MigrationGraph — pure graph queries (no connection)
# ---------------------------------------------------------------------------


class TestMigrationGraphPureQueries:
    def test_topological_order_returns_names_in_order(self) -> None:
        mods = [
            _module("0002_b", deps=["0001_a"]),
            _module("0001_a"),
        ]
        graph = MigrationGraph(mods)
        assert graph.topological_order() == ["0001_a", "0002_b"]

    def test_names_alias_of_topological_order(self) -> None:
        mods = [_module("0001_a"), _module("0002_b", deps=["0001_a"])]
        graph = MigrationGraph(mods)
        assert graph.names() == graph.topological_order()

    def test_modules_property_returns_migration_modules(self) -> None:
        mods = [_module("0001_a")]
        graph = MigrationGraph(mods)
        assert len(graph.modules) == 1
        assert graph.modules[0].name == "0001_a"

    def test_dependencies_of_returns_copy(self) -> None:
        mods = [
            _module("0001_a"),
            _module("0002_b", deps=["0001_a"]),
        ]
        graph = MigrationGraph(mods)
        deps = graph.dependencies_of("0002_b")
        assert deps == ["0001_a"]
        deps.append("HACK")
        # Mutation of the returned list must not affect the graph.
        assert graph.dependencies_of("0002_b") == ["0001_a"]

    def test_dependencies_of_unknown_name_raises(self) -> None:
        graph = MigrationGraph([_module("0001_a")])
        with pytest.raises(FerrumMigrationError, match="not in this graph"):
            graph.dependencies_of("0099_ghost")

    def test_detect_cycle_on_constructed_graph_returns_none(self) -> None:
        # The constructor already raises on cycles via topological_sort, so a
        # successfully-constructed graph is always acyclic.
        graph = MigrationGraph([_module("0001_a")])
        assert graph.detect_cycle() is None

    def test_constructor_propagates_cycle_error(self) -> None:
        mods = [
            _module("0001_a", deps=["0002_b"]),
            _module("0002_b", deps=["0001_a"]),
        ]
        with pytest.raises(ValueError, match=r"[Cc]ycle"):
            MigrationGraph(mods)


# ---------------------------------------------------------------------------
# MigrationGraph — ledger-backed queries (mocked connection)
# ---------------------------------------------------------------------------


def _mock_conn(applied: dict[str, str] | None = None) -> tuple[MagicMock, AsyncMock]:
    """Build a mock Connection whose ``find_applied_digest_by_name`` returns *applied*.

    Returns (conn, fetchrow_mock) so tests can also assert on call patterns.
    """
    applied = applied or {}
    conn = MagicMock()
    fetchrow = AsyncMock()

    async def _side_effect(sql: str, name: str) -> object | None:
        if name in applied:
            return {"digest": applied[name]}
        return None

    fetchrow.side_effect = _side_effect
    driver = MagicMock()
    driver.fetchrow = fetchrow
    conn._require_driver.return_value = driver
    return conn, fetchrow


class TestMigrationGraphStatus:
    @pytest.mark.asyncio
    async def test_no_connection_reports_unknown(self, tmp_path: Path) -> None:
        p = _write_migration_file(tmp_path, "0001_a.py")
        mod = MigrationModule(name="0001_a", path=p, migration=_migration_class())
        graph = MigrationGraph([mod], conn=None)
        statuses = await graph.status()
        assert len(statuses) == 1
        assert statuses[0].state == "unknown"
        assert statuses[0].reversible is False
        assert statuses[0].has_destructive_reverse is False

    @pytest.mark.asyncio
    async def test_pending_when_not_in_ledger(self, tmp_path: Path) -> None:
        p = _write_migration_file(tmp_path, "0001_a.py")
        mod = MigrationModule(name="0001_a", path=p, migration=_migration_class())
        conn, _ = _mock_conn(applied={})
        graph = MigrationGraph([mod], conn=conn)
        statuses = await graph.status()
        assert statuses[0].state == "pending"
        assert statuses[0].stored_digest == ""

    @pytest.mark.asyncio
    async def test_applied_when_digest_matches(self, tmp_path: Path) -> None:
        from ferrum.migrations.ledger import compute_digest

        p = _write_migration_file(tmp_path, "0001_a.py")
        content = p.read_text(encoding="utf-8")
        digest = compute_digest("0001_a", content)
        mod = MigrationModule(name="0001_a", path=p, migration=_migration_class())
        conn, _ = _mock_conn(applied={"0001_a": digest})
        graph = MigrationGraph([mod], conn=conn)
        statuses = await graph.status()
        assert statuses[0].state == "applied"
        assert statuses[0].digest == digest
        assert statuses[0].stored_digest == digest

    @pytest.mark.asyncio
    async def test_checksum_mismatch_when_digest_differs(self, tmp_path: Path) -> None:
        p = _write_migration_file(tmp_path, "0001_a.py")
        mod = MigrationModule(name="0001_a", path=p, migration=_migration_class())
        # Stored digest does not match the on-disk content.
        conn, _ = _mock_conn(applied={"0001_a": "deadbeef"})
        graph = MigrationGraph([mod], conn=conn)
        statuses = await graph.status()
        assert statuses[0].state == "checksum_mismatch"
        assert statuses[0].stored_digest == "deadbeef"

    @pytest.mark.asyncio
    async def test_has_destructive_reverse_reflects_reverse_ops(self, tmp_path: Path) -> None:
        M = _migration_class(reverse=[operations.DropTable("t")])  # destructive
        p = _write_migration_file(tmp_path, "0001_a.py")
        mod = MigrationModule(name="0001_a", path=p, migration=M)
        conn, _ = _mock_conn(applied={})
        graph = MigrationGraph([mod], conn=conn)
        statuses = await graph.status()
        assert statuses[0].reversible is True
        assert statuses[0].has_destructive_reverse is True


class TestMigrationGraphUpgradePlan:
    @pytest.mark.asyncio
    async def test_all_pending_when_none_applied(self, tmp_path: Path) -> None:
        files = [
            _write_migration_file(tmp_path, "0001_a.py"),
            _write_migration_file(tmp_path, "0002_b.py", deps=["0001_a"]),
        ]
        mods = [MigrationModule(name=p.stem, path=p, migration=_migration_class()) for p in files]
        # Fix deps on second module since the file template writes deps=[].
        mods[1].migration.dependencies = ["0001_a"]
        conn, _ = _mock_conn(applied={})
        graph = MigrationGraph(mods, conn=conn)
        plan = await graph.upgrade_plan()
        assert [m.name for m in plan] == ["0001_a", "0002_b"]

    @pytest.mark.asyncio
    async def test_skips_applied(self, tmp_path: Path) -> None:
        from ferrum.migrations.ledger import compute_digest

        p1 = _write_migration_file(tmp_path, "0001_a.py")
        p2 = _write_migration_file(tmp_path, "0002_b.py", deps=["0001_a"])
        mod1 = MigrationModule(name="0001_a", path=p1, migration=_migration_class())
        mod2 = MigrationModule(name="0002_b", path=p2, migration=_migration_class(deps=["0001_a"]))
        digest1 = compute_digest("0001_a", p1.read_text(encoding="utf-8"))
        conn, _ = _mock_conn(applied={"0001_a": digest1})
        graph = MigrationGraph([mod1, mod2], conn=conn)
        plan = await graph.upgrade_plan()
        assert [m.name for m in plan] == ["0002_b"]

    @pytest.mark.asyncio
    async def test_target_limits_plan_inclusive(self, tmp_path: Path) -> None:
        p1 = _write_migration_file(tmp_path, "0001_a.py")
        p2 = _write_migration_file(tmp_path, "0002_b.py", deps=["0001_a"])
        p3 = _write_migration_file(tmp_path, "0003_c.py", deps=["0002_b"])
        mod1 = MigrationModule(name="0001_a", path=p1, migration=_migration_class())
        mod2 = MigrationModule(name="0002_b", path=p2, migration=_migration_class(deps=["0001_a"]))
        mod3 = MigrationModule(name="0003_c", path=p3, migration=_migration_class(deps=["0002_b"]))
        conn, _ = _mock_conn(applied={})
        graph = MigrationGraph([mod1, mod2, mod3], conn=conn)
        plan = await graph.upgrade_plan(target="0002_b")
        assert [m.name for m in plan] == ["0001_a", "0002_b"]

    @pytest.mark.asyncio
    async def test_target_already_applied_returns_empty(self, tmp_path: Path) -> None:
        from ferrum.migrations.ledger import compute_digest

        p1 = _write_migration_file(tmp_path, "0001_a.py")
        p2 = _write_migration_file(tmp_path, "0002_b.py", deps=["0001_a"])
        mod1 = MigrationModule(name="0001_a", path=p1, migration=_migration_class())
        mod2 = MigrationModule(name="0002_b", path=p2, migration=_migration_class(deps=["0001_a"]))
        digest1 = compute_digest("0001_a", p1.read_text(encoding="utf-8"))
        digest2 = compute_digest("0002_b", p2.read_text(encoding="utf-8"))
        conn, _ = _mock_conn(applied={"0001_a": digest1, "0002_b": digest2})
        graph = MigrationGraph([mod1, mod2], conn=conn)
        plan = await graph.upgrade_plan(target="0002_b")
        assert plan == []

    @pytest.mark.asyncio
    async def test_unknown_target_raises(self, tmp_path: Path) -> None:
        p = _write_migration_file(tmp_path, "0001_a.py")
        mod = MigrationModule(name="0001_a", path=p, migration=_migration_class())
        conn, _ = _mock_conn(applied={})
        graph = MigrationGraph([mod], conn=conn)
        with pytest.raises(FerrumMigrationError, match="not in this graph"):
            await graph.upgrade_plan(target="0099_ghost")


class TestMigrationGraphDowngradePlan:
    @pytest.mark.asyncio
    async def test_no_target_returns_last_applied(self, tmp_path: Path) -> None:
        from ferrum.migrations.ledger import compute_digest

        p1 = _write_migration_file(tmp_path, "0001_a.py")
        p2 = _write_migration_file(tmp_path, "0002_b.py", deps=["0001_a"])
        mod1 = MigrationModule(name="0001_a", path=p1, migration=_migration_class())
        mod2 = MigrationModule(
            name="0002_b",
            path=p2,
            migration=_migration_class(deps=["0001_a"], reverse=[operations.DropTable("t")]),
        )
        digest1 = compute_digest("0001_a", p1.read_text(encoding="utf-8"))
        digest2 = compute_digest("0002_b", p2.read_text(encoding="utf-8"))
        conn, _ = _mock_conn(applied={"0001_a": digest1, "0002_b": digest2})
        graph = MigrationGraph([mod1, mod2], conn=conn)
        plan = await graph.downgrade_plan()
        assert [m.name for m in plan] == ["0002_b"]

    @pytest.mark.asyncio
    async def test_target_reverts_applied_after_target(self, tmp_path: Path) -> None:
        from ferrum.migrations.ledger import compute_digest

        p1 = _write_migration_file(tmp_path, "0001_a.py")
        p2 = _write_migration_file(tmp_path, "0002_b.py", deps=["0001_a"])
        p3 = _write_migration_file(tmp_path, "0003_c.py", deps=["0002_b"])
        mod1 = MigrationModule(name="0001_a", path=p1, migration=_migration_class())
        mod2 = MigrationModule(
            name="0002_b",
            path=p2,
            migration=_migration_class(deps=["0001_a"], reverse=[operations.DropTable("t")]),
        )
        mod3 = MigrationModule(
            name="0003_c",
            path=p3,
            migration=_migration_class(deps=["0002_b"], reverse=[operations.DropTable("u")]),
        )
        d1 = compute_digest("0001_a", p1.read_text(encoding="utf-8"))
        d2 = compute_digest("0002_b", p2.read_text(encoding="utf-8"))
        d3 = compute_digest("0003_c", p3.read_text(encoding="utf-8"))
        conn, _ = _mock_conn(applied={"0001_a": d1, "0002_b": d2, "0003_c": d3})
        graph = MigrationGraph([mod1, mod2, mod3], conn=conn)
        plan = await graph.downgrade_plan(target="0001_a")
        # Revert everything after 0001_a, most recent first.
        assert [m.name for m in plan] == ["0003_c", "0002_b"]

    @pytest.mark.asyncio
    async def test_target_is_most_recent_returns_empty(self, tmp_path: Path) -> None:
        from ferrum.migrations.ledger import compute_digest

        p1 = _write_migration_file(tmp_path, "0001_a.py")
        p2 = _write_migration_file(tmp_path, "0002_b.py", deps=["0001_a"])
        mod1 = MigrationModule(name="0001_a", path=p1, migration=_migration_class())
        mod2 = MigrationModule(
            name="0002_b",
            path=p2,
            migration=_migration_class(deps=["0001_a"], reverse=[operations.DropTable("t")]),
        )
        d1 = compute_digest("0001_a", p1.read_text(encoding="utf-8"))
        d2 = compute_digest("0002_b", p2.read_text(encoding="utf-8"))
        conn, _ = _mock_conn(applied={"0001_a": d1, "0002_b": d2})
        graph = MigrationGraph([mod1, mod2], conn=conn)
        plan = await graph.downgrade_plan(target="0002_b")
        assert plan == []

    @pytest.mark.asyncio
    async def test_target_not_applied_returns_empty(self, tmp_path: Path) -> None:
        from ferrum.migrations.ledger import compute_digest

        p1 = _write_migration_file(tmp_path, "0001_a.py")
        p2 = _write_migration_file(tmp_path, "0002_b.py", deps=["0001_a"])
        mod1 = MigrationModule(name="0001_a", path=p1, migration=_migration_class())
        mod2 = MigrationModule(
            name="0002_b",
            path=p2,
            migration=_migration_class(deps=["0001_a"], reverse=[operations.DropTable("t")]),
        )
        d1 = compute_digest("0001_a", p1.read_text(encoding="utf-8"))
        conn, _ = _mock_conn(applied={"0001_a": d1})  # 0002_b not applied
        graph = MigrationGraph([mod1, mod2], conn=conn)
        # Target 0002_b (not applied): nothing to revert.
        plan = await graph.downgrade_plan(target="0002_b")
        assert plan == []

    @pytest.mark.asyncio
    async def test_irreversible_in_plan_raises(self, tmp_path: Path) -> None:
        from ferrum.migrations.ledger import compute_digest

        p1 = _write_migration_file(tmp_path, "0001_a.py")
        p2 = _write_migration_file(tmp_path, "0002_b.py", deps=["0001_a"])
        mod1 = MigrationModule(name="0001_a", path=p1, migration=_migration_class())
        # 0002_b has empty reverse_operations → irreversible.
        mod2 = MigrationModule(name="0002_b", path=p2, migration=_migration_class(deps=["0001_a"]))
        d1 = compute_digest("0001_a", p1.read_text(encoding="utf-8"))
        d2 = compute_digest("0002_b", p2.read_text(encoding="utf-8"))
        conn, _ = _mock_conn(applied={"0001_a": d1, "0002_b": d2})
        graph = MigrationGraph([mod1, mod2], conn=conn)
        # Default revert (last applied = 0002_b) must raise because 0002_b is irreversible.
        with pytest.raises(FerrumMigrationError, match="irreversible"):
            await graph.downgrade_plan()

    @pytest.mark.asyncio
    async def test_unknown_target_raises(self, tmp_path: Path) -> None:
        p = _write_migration_file(tmp_path, "0001_a.py")
        mod = MigrationModule(name="0001_a", path=p, migration=_migration_class())
        conn, _ = _mock_conn(applied={})
        graph = MigrationGraph([mod], conn=conn)
        with pytest.raises(FerrumMigrationError, match="not in this graph"):
            await graph.downgrade_plan(target="0099_ghost")

    @pytest.mark.asyncio
    async def test_no_applied_returns_empty(self, tmp_path: Path) -> None:
        p = _write_migration_file(tmp_path, "0001_a.py")
        mod = MigrationModule(name="0001_a", path=p, migration=_migration_class())
        conn, _ = _mock_conn(applied={})
        graph = MigrationGraph([mod], conn=conn)
        assert await graph.downgrade_plan() == []


class TestMigrationGraphRecoveryGuidance:
    @pytest.mark.asyncio
    async def test_checksum_mismatch_hint(self, tmp_path: Path) -> None:
        p = _write_migration_file(tmp_path, "0001_a.py")
        mod = MigrationModule(name="0001_a", path=p, migration=_migration_class())
        conn, _ = _mock_conn(applied={"0001_a": "deadbeef"})
        graph = MigrationGraph([mod], conn=conn)
        hints = await graph.recovery_guidance()
        assert any("0001_a" in h and "edited" in h for h in hints)

    @pytest.mark.asyncio
    async def test_no_hints_when_clean(self, tmp_path: Path) -> None:
        from ferrum.migrations.ledger import compute_digest

        p = _write_migration_file(tmp_path, "0001_a.py")
        digest = compute_digest("0001_a", p.read_text(encoding="utf-8"))
        # A reversible migration with no destructive reverse ops is fully clean.
        mod = MigrationModule(
            name="0001_a",
            path=p,
            migration=_migration_class(reverse=[operations.RenameColumn("t", "a", "b")]),
        )
        conn, _ = _mock_conn(applied={"0001_a": digest})
        graph = MigrationGraph([mod], conn=conn)
        hints = await graph.recovery_guidance()
        assert hints == []

    @pytest.mark.asyncio
    async def test_irreversible_head_hint(self, tmp_path: Path) -> None:
        from ferrum.migrations.ledger import compute_digest

        p = _write_migration_file(tmp_path, "0001_a.py")
        digest = compute_digest("0001_a", p.read_text(encoding="utf-8"))
        mod = MigrationModule(name="0001_a", path=p, migration=_migration_class())
        conn, _ = _mock_conn(applied={"0001_a": digest})
        graph = MigrationGraph([mod], conn=conn)
        hints = await graph.recovery_guidance()
        assert any("irreversible" in h for h in hints)

    @pytest.mark.asyncio
    async def test_out_of_order_hint_when_dependency_not_applied(self, tmp_path: Path) -> None:
        from ferrum.migrations.ledger import compute_digest

        p1 = _write_migration_file(tmp_path, "0001_a.py")
        p2 = _write_migration_file(tmp_path, "0002_b.py", deps=["0001_a"])
        mod1 = MigrationModule(name="0001_a", path=p1, migration=_migration_class())
        mod2 = MigrationModule(name="0002_b", path=p2, migration=_migration_class(deps=["0001_a"]))
        # Only 0002_b is recorded as applied — its dependency 0001_a is not.
        d2 = compute_digest("0002_b", p2.read_text(encoding="utf-8"))
        conn, _ = _mock_conn(applied={"0002_b": d2})
        graph = MigrationGraph([mod1, mod2], conn=conn)
        hints = await graph.recovery_guidance()
        assert any("0001_a" in h and "0002_b" in h for h in hints)


# ---------------------------------------------------------------------------
# DataMigration — transaction policy + untrusted-source rejection
# ---------------------------------------------------------------------------


class _RecordingDataMigration(DataMigration):
    """Records whether it ran inside a Transaction or on a bare Connection.

    Uses an attribute marker set by the test's transaction context manager
    so the recording does not depend on ``type(conn).__name__`` (which is
    ``MagicMock`` for mocked transactions).
    """

    ran_on_transaction: bool = False
    ran: bool = False

    async def run(self, conn: object) -> None:
        self.ran = True
        # A real Transaction carries a ``_is_ferrum_transaction`` marker;
        # the test's fake transaction sets the same marker.
        self.ran_on_transaction = bool(getattr(conn, "_is_ferrum_transaction", False))


class _FailingDataMigration(DataMigration):
    async def run(self, conn: object) -> None:
        raise RuntimeError("data migration boom")


class _UntrustedDataMigration(DataMigration):
    is_trusted: ClassVar[bool] = False

    async def run(self, conn: object) -> None:
        raise AssertionError("untrusted data migration should never run")


class _BadPolicyDataMigration(DataMigration):
    transaction_policy: ClassVar[str] = "weird"

    async def run(self, conn: object) -> None:
        raise AssertionError("bad policy should be rejected before run")


class TestRunDataMigration:
    @pytest.mark.asyncio
    async def test_required_policy_wraps_in_transaction(self) -> None:
        from ferrum.connection import Connection

        conn = MagicMock(spec=Connection)
        tx = MagicMock()
        tx._is_ferrum_transaction = True  # marker the recording migration checks

        @asynccontextmanager
        async def _tx(*args: object, **kwargs: object) -> AsyncIterator[object]:
            yield tx

        conn.transaction = _tx
        migration = _RecordingDataMigration()
        await run_data_migration(conn, migration)
        assert migration.ran is True
        assert migration.ran_on_transaction is True

    @pytest.mark.asyncio
    async def test_none_policy_runs_on_connection_directly(self) -> None:
        from ferrum.connection import Connection

        conn = MagicMock(spec=Connection)
        conn.transaction = MagicMock(side_effect=AssertionError("none policy must not open a tx"))

        class _NonePolicy(_RecordingDataMigration):
            transaction_policy: ClassVar[str] = "none"

        migration = _NonePolicy()
        await run_data_migration(conn, migration)
        assert migration.ran is True
        assert migration.ran_on_transaction is False

    @pytest.mark.asyncio
    async def test_untrusted_refused_before_run(self) -> None:
        conn = MagicMock()
        migration = _UntrustedDataMigration()
        with pytest.raises(FerrumMigrationError, match="untrusted"):
            await run_data_migration(conn, migration)

    @pytest.mark.asyncio
    async def test_unknown_policy_rejected(self) -> None:
        conn = MagicMock()
        migration = _BadPolicyDataMigration()
        with pytest.raises(FerrumMigrationError, match="transaction_policy"):
            await run_data_migration(conn, migration)

    @pytest.mark.asyncio
    async def test_required_policy_failure_wraps_error(self) -> None:
        from ferrum.connection import Connection

        conn = MagicMock(spec=Connection)
        tx = MagicMock()

        @asynccontextmanager
        async def _tx(*args: object, **kwargs: object) -> AsyncIterator[object]:
            yield tx

        conn.transaction = _tx
        migration = _FailingDataMigration()
        with pytest.raises(FerrumMigrationError, match="failed inside its transaction"):
            await run_data_migration(conn, migration)

    @pytest.mark.asyncio
    async def test_none_policy_failure_wraps_error(self) -> None:
        from ferrum.connection import Connection

        conn = MagicMock(spec=Connection)

        class _NoneFailing(_FailingDataMigration):
            transaction_policy: ClassVar[str] = "none"

        migration = _NoneFailing()
        with pytest.raises(FerrumMigrationError, match="failed outside a transaction"):
            await run_data_migration(conn, migration)


# ---------------------------------------------------------------------------
# generate_offline_sql — checksums, phase annotations, ordering
# ---------------------------------------------------------------------------


_OFFLINE_FILE_TEMPLATE = """\
from ferrum.migrations import Migration
from ferrum.migrations import operations as _ops


class Migration(Migration):
    dependencies = {deps!r}
    operations = [
        _ops.CreateTable("{table}", [
            _ops.Column("id", "BIGSERIAL", primary_key=True, not_null=True),
        ]),
    ]
    reverse_operations = [
        _ops.DropTable("{table}"),
    ]
"""


def _write_offline_migration(
    dir_path: Path,
    filename: str,
    *,
    table: str,
    deps: list[str] | None = None,
) -> Path:
    p = dir_path / filename
    p.write_text(_OFFLINE_FILE_TEMPLATE.format(deps=deps or [], table=table))
    return p


class TestGenerateOfflineSql:
    def test_returns_offline_sql_plan(self, tmp_path: Path) -> None:
        _write_offline_migration(tmp_path, "0001_a.py", table="t1")
        modules = scan(tmp_path)
        plan = generate_offline_sql(modules)
        assert isinstance(plan, OfflineSqlPlan)
        assert plan.dialect == "postgres"
        assert len(plan.migrations) == 1

    def test_per_migration_digest_matches_ledger(self, tmp_path: Path) -> None:
        from ferrum.migrations.ledger import compute_digest

        p = _write_offline_migration(tmp_path, "0001_a.py", table="t1")
        modules = scan(tmp_path)
        plan = generate_offline_sql(modules)
        expected = compute_digest("0001_a", p.read_text(encoding="utf-8"))
        assert plan.migrations[0].digest == expected

    def test_topological_order_preserved(self, tmp_path: Path) -> None:
        _write_offline_migration(tmp_path, "0001_a.py", table="t1")
        _write_offline_migration(tmp_path, "0002_b.py", table="t2", deps=["0001_a"])
        modules = scan(tmp_path)
        plan = generate_offline_sql(modules)
        assert [m.name for m in plan.migrations] == ["0001_a", "0002_b"]

    def test_phase_annotations_pre_tx_tx_post_tx(self, tmp_path: Path) -> None:
        # Create a migration with a non-transactional op (create_extension)
        # followed by a transactional op (create_table) followed by another
        # non-transactional op (add_index concurrently).
        template = """\
from ferrum.migrations import Migration
from ferrum.migrations import operations


class Migration(Migration):
    dependencies = []
    operations = [
        operations.CreateExtension("pgcrypto"),
        operations.CreateTable("t1", [
            operations.Column("id", "BIGSERIAL", primary_key=True, not_null=True),
        ]),
        operations.AddIndex("t1", "idx_t1_id", ["id"], concurrently=True),
    ]
    reverse_operations = []
"""
        (tmp_path / "0001_a.py").write_text(template)
        modules = scan(tmp_path)
        plan = generate_offline_sql(modules)
        phases = plan.migrations[0].phases
        # pre_tx: create_extension; tx: create_table; post_tx: add_index concurrently.
        pre = [p for p in phases if p.phase == "pre_tx"]
        tx = [p for p in phases if p.phase == "tx"]
        post = [p for p in phases if p.phase == "post_tx"]
        assert len(pre) == 1
        assert pre[0].kind == "create_extension"
        assert len(tx) == 1
        assert tx[0].kind == "create_table"
        assert len(post) == 1
        assert post[0].kind == "add_index"

    def test_reversible_flag_set_from_reverse_operations(self, tmp_path: Path) -> None:
        _write_offline_migration(tmp_path, "0001_a.py", table="t1")
        modules = scan(tmp_path)
        plan = generate_offline_sql(modules)
        assert plan.migrations[0].reversible is True

    def test_reversible_false_when_no_reverse_operations(self, tmp_path: Path) -> None:
        template = """\
from ferrum.migrations import Migration
from ferrum.migrations import operations


class Migration(Migration):
    dependencies = []
    operations = [
        operations.CreateTable("t1", [
            operations.Column("id", "BIGSERIAL", primary_key=True, not_null=True),
        ]),
    ]
    reverse_operations = []
"""
        (tmp_path / "0001_a.py").write_text(template)
        modules = scan(tmp_path)
        plan = generate_offline_sql(modules)
        assert plan.migrations[0].reversible is False

    def test_has_destructive_flag_for_drop_table(self, tmp_path: Path) -> None:
        template = """\
from ferrum.migrations import Migration
from ferrum.migrations import operations


class Migration(Migration):
    dependencies = []
    operations = [
        operations.DropTable("t1"),
    ]
    reverse_operations = []
"""
        (tmp_path / "0001_a.py").write_text(template)
        modules = scan(tmp_path)
        plan = generate_offline_sql(modules)
        assert plan.migrations[0].has_destructive is True

    def test_has_destructive_false_for_safe_ops(self, tmp_path: Path) -> None:
        _write_offline_migration(tmp_path, "0001_a.py", table="t1")
        modules = scan(tmp_path)
        plan = generate_offline_sql(modules)
        assert plan.migrations[0].has_destructive is False

    def test_sql_statements_present(self, tmp_path: Path) -> None:
        _write_offline_migration(tmp_path, "0001_a.py", table="t1")
        modules = scan(tmp_path)
        plan = generate_offline_sql(modules)
        # Each phase carries rendered SQL.
        assert all(p.sql for p in plan.migrations[0].phases)
        # The tx phase for create_table must contain "CREATE TABLE".
        tx_sql = next(p.sql for p in plan.migrations[0].phases if p.phase == "tx")
        assert "CREATE TABLE" in tx_sql

    def test_dialect_param_passes_through(self, tmp_path: Path) -> None:
        _write_offline_migration(tmp_path, "0001_a.py", table="t1")
        modules = scan(tmp_path)
        plan = generate_offline_sql(modules, dialect="mysql")
        assert plan.dialect == "mysql"
        # MySQL emits ENGINE=InnoDB on CREATE TABLE.
        tx_sql = next(p.sql for p in plan.migrations[0].phases if p.phase == "tx")
        assert "ENGINE=InnoDB" in tx_sql
