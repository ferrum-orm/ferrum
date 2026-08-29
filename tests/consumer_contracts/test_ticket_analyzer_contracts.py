"""Live-PostgreSQL contract tests for Ticket Analyzer patterns not already
covered by ``tests/python/integration/test_ticket_analyzer_compat.py``.

Covers manifest entries: ta-02 (platform-admin RLS bypass), ta-04/ta-05
(CAS/update_returning lease claim over a Q()-composed unlocked predicate),
ta-06 (JSONB ``__contains``), ta-09 (bulk_upsert batching + conflict update),
ta-10 (``stream()`` bounded chunks), ta-12 (the ``filter(x=None)`` nullable-
predicate defect), ta-13 (``group_by`` + ``aggregate``).

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
    fixture so the ta-15 EnableRLS(force=True) workaround (two ops, see
    below) does not leak FORCE semantics onto the six non-RLS contract
    tests, which write through the plain ``pg_conn`` with no tenant GUC
    bound and would otherwise be blocked by the owner-inclusive FORCE
    policy.
    """
    await _apply_schema(pg_conn, suffix=suffix, team_table=team_table, event_table=event_table)
    operations = [
        # ta-15-migration-force-rls-never-enables (Ferrum defect): EnableRLS(
        # force=True) alone emits *only* `FORCE ROW LEVEL SECURITY`, never
        # `ENABLE ROW LEVEL SECURITY` — see orchestrator.py's enable_rls
        # branch (`if op.get("force"): return ...FORCE...` with no `else`
        # arm that also emits ENABLE). Since relrowsecurity stays false, the
        # policies below are a silent no-op and RLS provides zero isolation
        # — reproduced directly against live PostgreSQL by
        # test_force_rls_without_enable_rls_grants_no_isolation_defect.
        # Workaround: plain EnableRLS turns relrowsecurity on, and a second
        # force=True call additionally sets relforcerowsecurity.
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
    unique_suffix: str,
    require_native: None,
):
    Team, Event, team_table, event_table = _make_models(unique_suffix)
    await _apply_rls_schema(
        pg_conn, suffix=unique_suffix, team_table=team_table, event_table=event_table
    )
    try:
        yield {"Team": Team, "Event": Event, "team_table": team_table, "event_table": event_table}
    finally:
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
    """Manifest ta-02: admin=True on tenant_transaction() must bypass team_isolation."""
    Team = rls_contract_models["Team"]
    Event = rls_contract_models["Event"]

    team_a = await _create_team(pg_conn, Team, name="A")
    team_b = await _create_team(pg_conn, Team, name="B")
    async with tenant_transaction(pg_conn, team_a.id) as tx:
        await Event.objects.create(
            tx, id=uuid.uuid4(), team_id=team_a.id, dedup_key="a1", status="pending", tags={}
        )
    async with tenant_transaction(pg_conn, team_b.id) as tx:
        await Event.objects.create(
            tx, id=uuid.uuid4(), team_id=team_b.id, dedup_key="b1", status="pending", tags={}
        )

    # A plain team-scoped transaction sees only its own team's row.
    async with tenant_transaction(pg_conn, team_a.id) as tx:
        scoped = await Event.objects.all(tx)
        assert {row.team_id for row in scoped} == {team_a.id}

    # The platform-admin bypass transaction sees rows across both teams.
    async with tenant_transaction(pg_conn, team_a.id, admin=True) as tx:
        all_rows = await Event.objects.all(tx)
        assert {row.team_id for row in all_rows} == {team_a.id, team_b.id}


@pytest.mark.integration
@pytest.mark.xfail(
    reason=(
        "ta-15-migration-force-rls-never-enables (Ferrum defect): "
        "orchestrator.py's enable_rls branch for EnableRLS(force=True) emits "
        "only `ALTER TABLE ... FORCE ROW LEVEL SECURITY`, never the required "
        "`ALTER TABLE ... ENABLE ROW LEVEL SECURITY`. Postgres leaves "
        "relrowsecurity false in that state, so the team_isolation policy "
        "below is never evaluated and every row is visible with zero tenant "
        "GUC bound — a complete, silent RLS bypass for any consumer (e.g. "
        "Ticket Analyzer) that calls EnableRLS(force=True) expecting it to "
        "also enable RLS the way ENABLE + FORCE normally would."
    ),
    strict=True,
)
async def test_force_rls_without_enable_rls_grants_no_isolation_defect(
    pg_conn: ferrum.connection.Connection,
    unique_suffix: str,
    require_native: None,
) -> None:
    """Reproduces ta-15 directly: EnableRLS(force=True) alone must not leave
    relrowsecurity off. With the defect, a query issued with *no* app.team_id
    GUC bound at all returns rows from every team instead of zero rows.
    """
    Team, Event, team_table, event_table = _make_models(unique_suffix)
    await _apply_schema(
        pg_conn, suffix=unique_suffix, team_table=team_table, event_table=event_table
    )
    try:
        # Deliberately omit the plain EnableRLS(event_table) op — this single
        # force=True op is exactly what a consumer would write, expecting it
        # to both enable and force RLS.
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
        try:
            team_a = await _create_team(pg_conn, Team, name="A")
            team_b = await _create_team(pg_conn, Team, name="B")
            await Event.objects.create(
                pg_conn, id=uuid.uuid4(), team_id=team_a.id, dedup_key="a1", status="pending"
            )
            await Event.objects.create(
                pg_conn, id=uuid.uuid4(), team_id=team_b.id, dedup_key="b1", status="pending"
            )

            # No tenant_transaction / GUC bound at all. With RLS correctly
            # enabled, a policy comparing team_id to an unset GUC matches
            # nothing, so the correct, non-defective result is zero rows.
            leaked = await Event.objects.all(pg_conn)
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
        await _drop_schema(
            pg_conn, suffix=unique_suffix, team_table=team_table, event_table=event_table
        )


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
async def test_filter_equals_none_does_not_match_null_rows_defect(
    pg_conn: ferrum.connection.Connection,
    contract_models: dict[str, Any],
) -> None:
    """Manifest ta-12 (Ferrum defect): filter(x=None) binds SQL NULL to '=',
    which never matches per three-valued logic, unlike Django's filter(x=None)
    (which auto-translates to IS NULL). __is_null=True is required instead.
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

    # The defect: an equality filter against None finds nothing, even though a
    # matching NULL row exists.
    via_eq_none = await Event.objects.filter(team_id=team.id, locked_until=None).all(pg_conn)
    assert via_eq_none == []


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
