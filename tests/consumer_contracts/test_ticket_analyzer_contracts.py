"""Live-PostgreSQL contract tests for Ticket Analyzer patterns not already
covered by ``tests/python/integration/test_ticket_analyzer_compat.py``.

Covers manifest entries: ta-02 (platform-admin RLS bypass), ta-04/ta-05
(CAS/update_returning lease claim over a Q()-composed unlocked predicate),
ta-06 (JSONB ``__contains``), ta-09 (bulk_upsert batching + conflict update),
ta-10 (``stream()`` bounded chunks), ta-12 (``filter(x=None)`` nullable-
predicate Django-parity), ta-13 (``group_by`` + ``aggregate``).

Already covered elsewhere and intentionally not re-tested here: RLS
tenant_transaction (ta-01), composite PK (ta-03), pgvector (ta-08),
call_function (ta-11), UUID array + plain JSONB round trip (ta-07) — see
``test_ticket_analyzer_compat.py``.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, ClassVar
from uuid import UUID

import pytest
import pytest_asyncio

import ferrum
from ferrum.expressions import Q
from ferrum.migrations import apply
from ferrum.migrations import operations as ops
from ferrum.queryset import Aggregate
from ferrum.session import tenant_transaction


async def _create_rls_role(
    pg_conn: ferrum.connection.Connection,
    *,
    role_name: str,
    team_table: str,
    event_table: str,
) -> str:
    """Create a non-superuser non-BYPASSRLS role for RLS enforcement tests (W1-C).

    The role is granted all DML privileges on the test tables and USAGE on the
    public schema. The table owner stays the superuser; FORCE RLS makes the
    non-superuser role subject to the policies.
    """
    driver = pg_conn._require_driver()
    await driver.execute(f'DROP ROLE IF EXISTS "{role_name}"')
    await driver.execute(f"CREATE ROLE \"{role_name}\" LOGIN PASSWORD 'ferrum_rls'")
    await driver.execute(f'GRANT ALL ON "{team_table}" TO "{role_name}"')
    await driver.execute(f'GRANT ALL ON "{event_table}" TO "{role_name}"')
    await driver.execute(f'GRANT USAGE ON SCHEMA public TO "{role_name}"')
    return role_name


async def _drop_rls_role(
    pg_conn: ferrum.connection.Connection,
    *,
    role_name: str,
    team_table: str,
    event_table: str,
) -> None:
    """Revoke privileges and drop the non-superuser role (cleanup)."""
    driver = pg_conn._require_driver()
    await driver.execute(f'REVOKE ALL ON "{team_table}" FROM "{role_name}"')
    await driver.execute(f'REVOKE ALL ON "{event_table}" FROM "{role_name}"')
    await driver.execute(f'REVOKE USAGE ON SCHEMA public FROM "{role_name}"')
    await driver.execute(f'DROP ROLE IF EXISTS "{role_name}"')


def _plan(name: str, operations: list[Any]) -> str:
    return json.dumps(
        {
            "name": name,
            "version": "1",
            "requires_confirmation": False,
            "ops": [op.to_op_dict() for op in operations],
        }
    )


def _unlocked(now: datetime) -> Q:
    """Mirrors webhook_events_crud._UNLOCKED from ticket-analyzer-agent."""
    return Q(locked_until__is_null=True) | Q(locked_until__lt=now)


def _make_models(suffix: str) -> tuple[type, type, str, str]:
    team_table = f"cc_ta_teams_{suffix}"
    event_table = f"cc_ta_events_{suffix}"

    class Team(ferrum.Model):
        class Meta:
            table = team_table

        id: Annotated[UUID, ferrum.Field(primary_key=True, uuid_generate="v7")]
        name: str = ""

    class Event(ferrum.Model):
        class Meta:
            table = event_table
            indexes: ClassVar[list[ferrum.Index]] = [
                ferrum.Index(fields=("team_id", "dedup_key"), unique=True),
            ]

        id: Annotated[UUID, ferrum.Field(primary_key=True, uuid_generate="v7")]
        team_id: UUID
        dedup_key: str = ""
        category: str = ""
        status: str = "pending"
        attempts: int = 0
        locked_until: datetime | None = None
        tags: Annotated[dict, ferrum.Field(default_factory=dict)] = ferrum.Field(  # type: ignore[assignment]
            default_factory=dict
        )

    return Team, Event, team_table, event_table


async def _apply_schema(
    pg_conn: ferrum.connection.Connection,
    *,
    suffix: str,
    team_table: str,
    event_table: str,
) -> None:
    operations = [
        ops.CreateTable(
            team_table,
            [
                ops.Column("id", "UUID", not_null=True, primary_key=True),
                ops.Column("name", "TEXT", not_null=True, default="''"),
            ],
        ),
        ops.CreateTable(
            event_table,
            [
                ops.Column("id", "UUID", not_null=True, primary_key=True),
                ops.Column("team_id", "UUID", not_null=True),
                ops.Column("dedup_key", "TEXT", not_null=True, default="''"),
                ops.Column("category", "TEXT", not_null=True, default="''"),
                # No SQL-level DEFAULT for status/tags: ferrum's DDL DEFAULT
                # allowlist rejects non-empty quoted string literals such as
                # "'pending'" (see ta-15-migration-default-string-literal, a
                # Ferrum defect). Every ORM create() call below sends every
                # field explicitly, so NOT NULL alone is sufficient here.
                ops.Column("status", "TEXT", not_null=True),
                ops.Column("attempts", "INTEGER", not_null=True, default="0"),
                ops.Column("locked_until", "TIMESTAMPTZ"),
                ops.Column("tags", "JSONB", not_null=True),
            ],
        ),
        ops.AddIndex(
            event_table,
            f"idx_{event_table}_team_dedup",
            ["team_id", "dedup_key"],
            unique=True,
        ),
    ]
    await apply(pg_conn, _plan(f"cc_ta_{suffix}", operations), dry_run=False)


async def _drop_schema(
    pg_conn: ferrum.connection.Connection,
    *,
    suffix: str,
    team_table: str,
    event_table: str,
) -> None:
    drop_ops = [
        ops.DropTable(event_table),
        ops.DropTable(team_table),
    ]
    await apply(pg_conn, _plan(f"drop_cc_ta_{suffix}", drop_ops), dry_run=False, confirm=True)


@pytest_asyncio.fixture
async def contract_models(
    pg_conn: ferrum.connection.Connection,
    unique_suffix: str,
    require_native: None,
):
    Team, Event, team_table, event_table = _make_models(unique_suffix)
    await _apply_schema(
        pg_conn, suffix=unique_suffix, team_table=team_table, event_table=event_table
    )
    try:
        yield {"Team": Team, "Event": Event, "team_table": team_table, "event_table": event_table}
    finally:
        await _drop_schema(
            pg_conn, suffix=unique_suffix, team_table=team_table, event_table=event_table
        )


async def _apply_rls_schema(
    pg_conn: ferrum.connection.Connection,
    *,
    suffix: str,
    team_table: str,
    event_table: str,
) -> None:
    """Same schema as ``_apply_schema`` plus RLS, isolated into its own
    fixture so the FORCE RLS semantics do not leak onto the six non-RLS
    contract tests, which write through the plain ``pg_conn`` with no
    tenant GUC bound and would otherwise be blocked by the owner-inclusive
    FORCE policy.

    W1-C note: EnableRLS(force=True) now emits both ENABLE and FORCE, so
    the separate plain EnableRLS is no longer a required workaround. Both
    ops are kept for clarity and because the fixture predates the fix.
    """
    await _apply_schema(pg_conn, suffix=suffix, team_table=team_table, event_table=event_table)
    operations = [
        ops.EnableRLS(event_table),
        ops.EnableRLS(event_table, force=True),
        ops.CreatePolicy(
            "team_isolation",
            event_table,
            "team_id::text = current_setting('app.team_id', true)",
        ),
        ops.CreatePolicy(
            "platform_admin_bypass",
            event_table,
            "current_setting('app.platform_admin', true) = 'true'",
        ),
    ]
    await apply(pg_conn, _plan(f"cc_ta_rls_{suffix}", operations), dry_run=False)


async def _drop_rls_schema(
    pg_conn: ferrum.connection.Connection,
    *,
    suffix: str,
    team_table: str,
    event_table: str,
) -> None:
    drop_ops = [
        ops.DropPolicy("platform_admin_bypass", event_table),
        ops.DropPolicy("team_isolation", event_table),
        ops.DisableRLS(event_table),
    ]
    await apply(pg_conn, _plan(f"drop_cc_ta_rls_{suffix}", drop_ops), dry_run=False, confirm=True)
    await _drop_schema(pg_conn, suffix=suffix, team_table=team_table, event_table=event_table)


@pytest_asyncio.fixture
async def rls_contract_models(
    pg_conn: ferrum.connection.Connection,
    pg_dsn: str,
    unique_suffix: str,
    require_native: None,
):
    """RLS test fixture with a non-superuser connection (W1-C).

    The superuser ``pg_conn`` applies the schema + RLS policies. A separate
    non-superuser role ``ferrum_rls_<suffix>`` is created and granted DML
    privileges. The yielded dict includes ``rls_conn`` — a Ferrum connection
    logged in as that role — so RLS policies are actually enforced (FORCE RLS
    does not affect superusers, only the table owner and non-bypass roles).
    """
    Team, Event, team_table, event_table = _make_models(unique_suffix)
    await _apply_rls_schema(
        pg_conn, suffix=unique_suffix, team_table=team_table, event_table=event_table
    )
    role_name = f"ferrum_rls_{unique_suffix}"
    await _create_rls_role(
        pg_conn, role_name=role_name, team_table=team_table, event_table=event_table
    )
    # Build a DSN that logs in as the non-superuser role (no password —
    # PostgreSQL trust authentication on localhost).
    from urllib.parse import urlparse

    parsed = urlparse(pg_dsn)
    rls_dsn = f"postgresql://{role_name}:ferrum_rls@{parsed.hostname}:{parsed.port}{parsed.path}"
    async with ferrum.connect(rls_dsn) as rls_conn:
        try:
            yield {
                "Team": Team,
                "Event": Event,
                "team_table": team_table,
                "event_table": event_table,
                "rls_conn": rls_conn,
            }
        finally:
            await rls_conn.close()
    await _drop_rls_role(
        pg_conn, role_name=role_name, team_table=team_table, event_table=event_table
    )
    await _drop_rls_schema(
        pg_conn, suffix=unique_suffix, team_table=team_table, event_table=event_table
    )


async def _create_team(pg_conn: ferrum.connection.Connection, team_cls: type, *, name: str) -> Any:
    return await team_cls.objects.create(pg_conn, id=uuid.uuid4(), name=name)


@pytest.mark.integration
async def test_platform_admin_bypass_sees_all_teams(
    pg_conn: ferrum.connection.Connection,
    rls_contract_models: dict[str, Any],
) -> None:
    """Manifest ta-02: admin=True on tenant_transaction() must bypass team_isolation.

    W1-C: uses a non-superuser connection (rls_conn) so FORCE RLS is actually
    enforced. Superusers bypass RLS entirely, so the test would be a no-op
    with the default pg_conn.
    """
    Team = rls_contract_models["Team"]
    Event = rls_contract_models["Event"]
    rls_conn = rls_contract_models["rls_conn"]

    # Create teams and events using the superuser (pg_conn) — the non-superuser
    # role also has INSERT privileges.
    team_a = await _create_team(pg_conn, Team, name="A")
    team_b = await _create_team(pg_conn, Team, name="B")
    async with tenant_transaction(rls_conn, team_a.id) as tx:
        await Event.objects.create(
            tx, id=uuid.uuid4(), team_id=team_a.id, dedup_key="a1", status="pending", tags={}
        )
    async with tenant_transaction(rls_conn, team_b.id) as tx:
        await Event.objects.create(
            tx, id=uuid.uuid4(), team_id=team_b.id, dedup_key="b1", status="pending", tags={}
        )

    # A plain team-scoped transaction on the non-superuser connection sees only
    # its own team's row (RLS policy enforced because the role is non-superuser
    # and FORCE RLS is set).
    async with tenant_transaction(rls_conn, team_a.id) as tx:
        scoped = await Event.objects.all(tx)
        assert {row.team_id for row in scoped} == {team_a.id}

    # The platform-admin bypass transaction sees rows across both teams.
    async with tenant_transaction(rls_conn, team_a.id, admin=True) as tx:
        all_rows = await Event.objects.all(tx)
        assert {row.team_id for row in all_rows} == {team_a.id, team_b.id}


@pytest.mark.integration
async def test_force_rls_alone_enables_and_forces_rls(
    pg_conn: ferrum.connection.Connection,
    pg_dsn: str,
    unique_suffix: str,
    require_native: None,
) -> None:
    """W1-C / W0-B ta-16: EnableRLS(force=True) emits ENABLE then FORCE so
    relrowsecurity AND relforcerowsecurity are both true. A single force=True
    op is exactly what a consumer would write, expecting it to both enable
    and force RLS. With the fix, a query issued with *no* app.team_id GUC
    bound at all returns zero rows (the policy matches nothing) — verified
    on a non-superuser connection so RLS is actually enforced.
    """
    from urllib.parse import urlparse

    Team, Event, team_table, event_table = _make_models(unique_suffix)
    await _apply_schema(
        pg_conn, suffix=unique_suffix, team_table=team_table, event_table=event_table
    )
    role_name = f"ferrum_rls_force_{unique_suffix}"
    try:
        # Deliberately use only EnableRLS(force=True) — no plain EnableRLS first.
        # With the W1-C fix, this single op emits both ENABLE and FORCE.
        force_only_ops = [
            ops.EnableRLS(event_table, force=True),
            ops.CreatePolicy(
                "team_isolation",
                event_table,
                "team_id::text = current_setting('app.team_id', true)",
            ),
        ]
        await apply(
            pg_conn, _plan(f"cc_ta_force_only_{unique_suffix}", force_only_ops), dry_run=False
        )

        # Create the non-superuser role for RLS enforcement verification.
        await _create_rls_role(
            pg_conn, role_name=role_name, team_table=team_table, event_table=event_table
        )
        parsed = urlparse(pg_dsn)
        rls_dsn = (
            f"postgresql://{role_name}:ferrum_rls@{parsed.hostname}:{parsed.port}{parsed.path}"
        )

        try:
            # Verify pg_class flags directly: both relrowsecurity and
            # relforcerowsecurity must be true after the single force=True op.
            driver = pg_conn._require_driver()
            row = await driver.fetchrow(
                "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = $1",
                event_table,
            )
            assert row is not None, f"Table {event_table} not found in pg_class"
            relrowsecurity = row.get("relrowsecurity", row[0]) if isinstance(row, dict) else row[0]
            relforcerowsecurity = (
                row.get("relforcerowsecurity", row[1]) if isinstance(row, dict) else row[1]
            )
            assert relrowsecurity is True, (
                f"relrowsecurity should be true after EnableRLS(force=True), got {relrowsecurity!r}"
            )
            assert relforcerowsecurity is True, (
                f"relforcerowsecurity should be true after EnableRLS(force=True), "
                f"got {relforcerowsecurity!r}"
            )

            team_a = await _create_team(pg_conn, Team, name="A")
            team_b = await _create_team(pg_conn, Team, name="B")
            await Event.objects.create(
                pg_conn,
                id=uuid.uuid4(),
                team_id=team_a.id,
                dedup_key="a1",
                status="pending",
                tags={},
            )
            await Event.objects.create(
                pg_conn,
                id=uuid.uuid4(),
                team_id=team_b.id,
                dedup_key="b1",
                status="pending",
                tags={},
            )

            # Verify RLS enforcement on a non-superuser connection: with no
            # tenant_transaction / GUC bound, the policy matches nothing, so
            # the correct result is zero rows. Superusers bypass RLS, so we
            # must use the non-superuser role.
            async with ferrum.connect(rls_dsn) as rls_conn:
                leaked = await Event.objects.all(rls_conn)
                assert leaked == []
        finally:
            await apply(
                pg_conn,
                _plan(
                    f"drop_cc_ta_force_only_{unique_suffix}",
                    [
                        ops.DropPolicy("team_isolation", event_table),
                        ops.DisableRLS(event_table),
                    ],
                ),
                dry_run=False,
                confirm=True,
            )
    finally:
        await _drop_rls_role(
            pg_conn, role_name=role_name, team_table=team_table, event_table=event_table
        )
        await _drop_schema(
            pg_conn, suffix=unique_suffix, team_table=team_table, event_table=event_table
        )


@pytest.mark.integration
async def test_tenant_guc_does_not_leak_after_commit(
    pg_conn: ferrum.connection.Connection,
    rls_contract_models: dict[str, Any],
) -> None:
    """Manifest ta-01/ta-02 (GUC isolation): after a ``tenant_transaction``
    commits, the ``app.team_id`` GUC must NOT persist on the underlying pooled
    connection — a subsequent bare query (no GUC bound) must see zero rows
    under FORCE RLS, proving the GUC was reset to its default (empty).
    """
    Team = rls_contract_models["Team"]
    Event = rls_contract_models["Event"]
    rls_conn = rls_contract_models["rls_conn"]

    team_a = await _create_team(pg_conn, Team, name="A")
    async with tenant_transaction(rls_conn, team_a.id) as tx:
        await Event.objects.create(
            tx, id=uuid.uuid4(), team_id=team_a.id, dedup_key="a1", status="pending", tags={}
        )
        scoped = await Event.objects.all(tx)
        assert {row.team_id for row in scoped} == {team_a.id}

    # After commit, the GUC must be reset. A bare query through the same
    # connection (which is returned to the pool on transaction exit) must see
    # zero rows — the RLS policy matches nothing without app.team_id set.
    leaked = await Event.objects.all(rls_conn)
    assert leaked == []


@pytest.mark.integration
async def test_platform_admin_guc_does_not_leak_after_commit(
    pg_conn: ferrum.connection.Connection,
    rls_contract_models: dict[str, Any],
) -> None:
    """Manifest ta-02 (GUC isolation): after a platform-admin ``tenant_transaction``
    with ``admin=True`` commits, the ``app.platform_admin`` GUC must NOT persist
    on the underlying pooled connection — a subsequent bare query must see zero
    rows (not the cross-team rows the admin bypass saw), proving the admin GUC
    was reset.
    """
    Team = rls_contract_models["Team"]
    Event = rls_contract_models["Event"]
    rls_conn = rls_contract_models["rls_conn"]

    team_a = await _create_team(pg_conn, Team, name="A")
    team_b = await _create_team(pg_conn, Team, name="B")
    async with tenant_transaction(rls_conn, team_a.id) as tx:
        await Event.objects.create(
            tx, id=uuid.uuid4(), team_id=team_a.id, dedup_key="a1", status="pending", tags={}
        )
    async with tenant_transaction(rls_conn, team_b.id) as tx:
        await Event.objects.create(
            tx, id=uuid.uuid4(), team_id=team_b.id, dedup_key="b1", status="pending", tags={}
        )

    # Admin bypass sees both teams.
    async with tenant_transaction(rls_conn, team_a.id, admin=True) as tx:
        all_rows = await Event.objects.all(tx)
        assert {row.team_id for row in all_rows} == {team_a.id, team_b.id}

    # After commit, admin GUC must be reset. A bare query must see zero rows
    # (no team_id, no platform_admin) — not the cross-team view the admin saw.
    leaked = await Event.objects.all(rls_conn)
    assert leaked == []


@pytest.mark.integration
async def test_tenant_guc_does_not_leak_on_rollback(
    pg_conn: ferrum.connection.Connection,
    rls_contract_models: dict[str, Any],
) -> None:
    """Manifest ta-01 (GUC isolation on rollback): if a ``tenant_transaction``
    rolls back (via exception or explicit rollback), the ``app.team_id`` GUC
    must still reset — a subsequent bare query must see zero rows.
    """
    Team = rls_contract_models["Team"]
    Event = rls_contract_models["Event"]
    rls_conn = rls_contract_models["rls_conn"]

    team_a = await _create_team(pg_conn, Team, name="A")

    # Simulate a rollback: raise inside the tenant_transaction.
    with pytest.raises(RuntimeError, match="rollback-test"):
        async with tenant_transaction(rls_conn, team_a.id) as tx:
            await Event.objects.create(
                tx, id=uuid.uuid4(), team_id=team_a.id, dedup_key="a1", status="pending", tags={}
            )
            raise RuntimeError("rollback-test")

    # After rollback, the GUC must be reset. A bare query must see zero rows.
    leaked = await Event.objects.all(rls_conn)
    assert leaked == []


@pytest.mark.integration
async def test_cas_update_returning_lease_claim(
    pg_conn: ferrum.connection.Connection,
    contract_models: dict[str, Any],
) -> None:
    """Manifest ta-04/ta-05: filter(...).filter(_unlocked(now)).update_returning(...)
    as an optimistic-concurrency lease claim, mirroring webhook_events_crud.py.
    """
    Team = contract_models["Team"]
    Event = contract_models["Event"]
    team = await _create_team(pg_conn, Team, name="team")
    now = datetime.now(UTC)

    fresh = await Event.objects.create(
        pg_conn,
        id=uuid.uuid4(),
        team_id=team.id,
        dedup_key="fresh",
        status="pending",
        attempts=0,
        tags={},
    )
    expired_lock = await Event.objects.create(
        pg_conn,
        id=uuid.uuid4(),
        team_id=team.id,
        dedup_key="expired",
        status="processing",
        attempts=1,
        locked_until=now - timedelta(minutes=5),
        tags={},
    )

    # A never-locked row is claimable via the is_null branch of _unlocked().
    claimed = await (
        Event.objects.filter(id=fresh.id, status="pending", attempts=0)
        .filter(_unlocked(now))
        .update_returning(
            pg_conn, attempts=1, status="processing", locked_until=now + timedelta(minutes=1)
        )
    )
    assert len(claimed) == 1
    assert claimed[0]["attempts"] == 1

    # A stale-attempts re-claim of the same row (simulating a losing racer) returns
    # zero rows — the compare-and-set contract.
    stale_reclaim = await (
        Event.objects.filter(id=fresh.id, status="pending", attempts=0)
        .filter(_unlocked(now))
        .update_returning(pg_conn, attempts=2, status="processing")
    )
    assert stale_reclaim == []

    # A row whose lease has expired is claimable via the __lt branch of _unlocked(),
    # even though its status is not "pending".
    claimed_expired = await (
        Event.objects.filter(id=expired_lock.id, status="processing", attempts=1)
        .filter(_unlocked(now))
        .update_returning(pg_conn, attempts=2, locked_until=now + timedelta(minutes=1))
    )
    assert len(claimed_expired) == 1
    assert claimed_expired[0]["attempts"] == 2


@pytest.mark.integration
async def test_filter_equals_none_matches_null_rows_django_parity(
    pg_conn: ferrum.connection.Connection,
    contract_models: dict[str, Any],
) -> None:
    """Manifest ta-12 (resolved): ``filter(x=None)`` now compiles to ``IS NULL``
    via ``_normalize_null_lookup`` (Django-parity), not ``= NULL`` (which never
    matches per three-valued logic).  Previously a FERRUM_DEFECT; now SUPPORTED.

    Both ``filter(x=None)`` and ``filter(x__is_null=True)`` find NULL rows;
    ``exclude(x=None)`` finds non-NULL rows (IS NOT NULL).
    """
    Team = contract_models["Team"]
    Event = contract_models["Event"]
    team = await _create_team(pg_conn, Team, name="team")
    await Event.objects.create(
        pg_conn,
        id=uuid.uuid4(),
        team_id=team.id,
        dedup_key="null-row",
        locked_until=None,
        status="pending",
        tags={},
    )
    await Event.objects.create(
        pg_conn,
        id=uuid.uuid4(),
        team_id=team.id,
        dedup_key="non-null-row",
        locked_until=datetime.now(UTC),
        status="pending",
        tags={},
    )

    via_is_null = await Event.objects.filter(team_id=team.id, locked_until__is_null=True).all(
        pg_conn
    )
    assert len(via_is_null) == 1
    assert via_is_null[0].dedup_key == "null-row"

    # The fix: filter(x=None) now auto-translates to IS NULL (Django-parity),
    # finding the NULL row just like __is_null=True.
    via_eq_none = await Event.objects.filter(team_id=team.id, locked_until=None).all(pg_conn)
    assert len(via_eq_none) == 1
    assert via_eq_none[0].dedup_key == "null-row"

    # exclude(x=None) wraps the IS NULL rewrite in NOT(…) → IS NOT NULL.
    via_exclude_none = await Event.objects.exclude(team_id=team.id, locked_until=None).all(pg_conn)
    assert len(via_exclude_none) == 1
    assert via_exclude_none[0].dedup_key == "non-null-row"


@pytest.mark.integration
async def test_jsonb_contains_filter_uses_containment_operator(
    pg_conn: ferrum.connection.Connection,
    contract_models: dict[str, Any],
) -> None:
    """Manifest ta-06: __contains on a JSONB dict field must compile to @>, not
    a text LIKE, and must only match rows whose JSONB value actually contains
    the probe.
    """
    Team = contract_models["Team"]
    Event = contract_models["Event"]
    team = await _create_team(pg_conn, Team, name="team")
    await Event.objects.create(
        pg_conn,
        id=uuid.uuid4(),
        team_id=team.id,
        dedup_key="match",
        status="pending",
        tags={"env": "prod", "severity": "high"},
    )
    await Event.objects.create(
        pg_conn,
        id=uuid.uuid4(),
        team_id=team.id,
        dedup_key="no-match",
        status="pending",
        tags={"env": "staging"},
    )

    matched = await Event.objects.filter(team_id=team.id, tags__contains={"env": "prod"}).all(
        pg_conn
    )
    assert [row.dedup_key for row in matched] == ["match"]


@pytest.mark.integration
async def test_bulk_upsert_batches_and_updates_conflicts(
    pg_conn: ferrum.connection.Connection,
    contract_models: dict[str, Any],
) -> None:
    """Manifest ta-09: bulk_upsert with a static update_fields list, batched
    below the row count, both inserts new rows and overwrites conflicting
    rows — mirroring tickets_crud.bulk_upsert_tickets.
    """
    Team = contract_models["Team"]
    Event = contract_models["Event"]
    team = await _create_team(pg_conn, Team, name="team")

    first_batch = [
        Event.model_construct(
            id=uuid.uuid4(),
            team_id=team.id,
            dedup_key=f"dk-{i}",
            category="a",
            status="pending",
            attempts=0,
        )
        for i in range(3)
    ]
    inserted = await Event.objects.bulk_upsert(
        pg_conn,
        first_batch,
        conflict_fields=["team_id", "dedup_key"],
        update_fields=["category", "status"],
        batch_size=2,  # forces >1 statement across the 3-row batch
        returning=False,
    )
    assert inserted == 3

    # Re-upsert two existing dedup_keys with a new status/category plus one
    # brand-new dedup_key, still batched below the row count.
    second_batch = [
        Event.model_construct(
            id=uuid.uuid4(),
            team_id=team.id,
            dedup_key="dk-0",
            category="b",
            status="done",
            attempts=0,
        ),
        Event.model_construct(
            id=uuid.uuid4(),
            team_id=team.id,
            dedup_key="dk-1",
            category="b",
            status="done",
            attempts=0,
        ),
        Event.model_construct(
            id=uuid.uuid4(),
            team_id=team.id,
            dedup_key="dk-new",
            category="b",
            status="done",
            attempts=0,
        ),
    ]
    upserted = await Event.objects.bulk_upsert(
        pg_conn,
        second_batch,
        conflict_fields=["team_id", "dedup_key"],
        update_fields=["category", "status"],
        batch_size=2,
        returning=False,
    )
    assert upserted == 3

    all_rows = await Event.objects.filter(team_id=team.id).all(pg_conn)
    assert len(all_rows) == 4  # 3 original + 1 brand-new; dk-0/dk-1 updated in place
    by_key = {row.dedup_key: row for row in all_rows}
    assert by_key["dk-0"].status == "done"
    assert by_key["dk-1"].status == "done"
    assert by_key["dk-2"].status == "pending"  # untouched by the second batch
    assert by_key["dk-new"].category == "b"


@pytest.mark.integration
async def test_stream_yields_bounded_chunks_pinned_to_one_connection(
    pg_conn: ferrum.connection.Connection,
    contract_models: dict[str, Any],
) -> None:
    """Manifest ta-10: stream(conn, chunk_size=...) yields chunk_size-bounded
    chunks summing to the full matching row count, mirroring
    tickets_crud.iter_ticket_chunks.
    """
    Team = contract_models["Team"]
    Event = contract_models["Event"]
    team = await _create_team(pg_conn, Team, name="team")
    total_rows = 25
    chunk_size = 10
    for i in range(total_rows):
        await Event.objects.create(
            pg_conn,
            id=uuid.uuid4(),
            team_id=team.id,
            dedup_key=f"s-{i}",
            status="pending",
            tags={},
        )

    seen = 0
    chunk_lengths: list[int] = []
    async with Event.objects.filter(team_id=team.id).stream(
        pg_conn, chunk_size=chunk_size
    ) as chunks:
        async for chunk in chunks:
            assert len(chunk) <= chunk_size
            chunk_lengths.append(len(chunk))
            seen += len(chunk)

    assert seen == total_rows
    assert chunk_lengths == [10, 10, 5]


@pytest.mark.integration
async def test_group_by_aggregate_counts_rows_per_bucket(
    pg_conn: ferrum.connection.Connection,
    contract_models: dict[str, Any],
) -> None:
    """Manifest ta-13: group_by(...) + aggregate(...) returns correct
    per-bucket counts, the primitive behind ticket_counts_by_day /
    sql_aggregate(metric="by_category"|"by_severity").
    """
    Team = contract_models["Team"]
    Event = contract_models["Event"]
    team = await _create_team(pg_conn, Team, name="team")
    for i in range(3):
        await Event.objects.create(
            pg_conn,
            id=uuid.uuid4(),
            team_id=team.id,
            dedup_key=f"a-{i}",
            category="a",
            status="pending",
            tags={},
        )
    for i in range(2):
        await Event.objects.create(
            pg_conn,
            id=uuid.uuid4(),
            team_id=team.id,
            dedup_key=f"b-{i}",
            category="b",
            status="pending",
            tags={},
        )

    rows = await (
        Event.objects.filter(team_id=team.id)
        .group_by("category")
        .aggregate(pg_conn, rows=Aggregate.count())
    )
    by_category = {row["category"]: row["rows"] for row in rows}
    assert by_category == {"a": 3, "b": 2}
