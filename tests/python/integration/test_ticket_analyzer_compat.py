"""Integration tests for ticket-analyzer compatibility patterns (live PostgreSQL)."""

# ruff: noqa: S608 — table identifiers are test-controlled suffixes, not user input.

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any, ClassVar
from uuid import UUID

import pytest
import pytest_asyncio

import ferrum
from ferrum.ext.pgvector import vector_search
from ferrum.migrations import apply
from ferrum.migrations import operations as ops
from ferrum.session import current_setting, tenant_transaction

from .helpers import raw_pool


def _plan(name: str, operations: list) -> str:
    return json.dumps(
        {
            "name": name,
            "version": "1",
            "requires_confirmation": False,
            "ops": [op.to_op_dict() for op in operations],
        }
    )


def _make_models(suffix: str) -> tuple[type, type, type, type, str]:
    """Build model classes with isolated table names for parallel-safe tests."""

    team_table = f"ta_int_teams_{suffix}"
    ticket_table = f"ta_int_tickets_{suffix}"
    issue_table = f"ta_int_issues_{suffix}"
    alert_table = f"ta_int_alerts_{suffix}"
    echo_fn = f"ta_int_echo_{suffix}"

    class Team(ferrum.Model):
        class Meta:
            table = team_table

        id: Annotated[UUID, ferrum.Field(primary_key=True, uuid_generate="v7")]
        name: str = ""

    class Ticket(ferrum.Model):
        class Meta:
            table = ticket_table

        id: Annotated[int, ferrum.Field(primary_key=True)]
        first_seen_at: Annotated[datetime, ferrum.Field(primary_key=True)]
        team_id: UUID
        helpshift_id: str = ""
        summary: str = ""
        summary_embedding: Annotated[
            ferrum.Vector | None,
            ferrum.Field(vector_dimensions=8),
        ] = None

    class Issue(ferrum.Model):
        class Meta:
            table = issue_table
            indexes: ClassVar[list[ferrum.Index]] = [
                ferrum.Index(fields=("team_id", "dedup_key"), unique=True),
            ]

        id: Annotated[UUID, ferrum.Field(primary_key=True, uuid_generate="v7")]
        team_id: UUID
        dedup_key: str = ""
        title: str = ""
        summary: str = ""

    class Alert(ferrum.Model):
        class Meta:
            table = alert_table

        id: Annotated[UUID, ferrum.Field(primary_key=True, uuid_generate="v7")]
        team_id: UUID
        title: str = ""
        ticket_ids: list[UUID] = ferrum.Field(default_factory=list)
        slack_delivery: dict = ferrum.Field(default_factory=dict)

    return Team, Ticket, Issue, Alert, echo_fn


async def _apply_schema(
    pg_conn: ferrum.connection.Connection,
    *,
    suffix: str,
    team_table: str,
    ticket_table: str,
    issue_table: str,
    alert_table: str,
    echo_fn: str,
) -> None:
    echo_body = f"""CREATE OR REPLACE FUNCTION {echo_fn}(p_msg text)
RETURNS TABLE(result text) LANGUAGE sql AS $$
  SELECT p_msg;
$$;"""
    operations = [
        ops.CreateExtension("vector"),
        ops.CreateTable(
            team_table,
            [
                ops.Column("id", "UUID", not_null=True, primary_key=True),
                ops.Column("name", "TEXT", not_null=True, default="''"),
            ],
        ),
        ops.CreateTable(
            ticket_table,
            [
                ops.Column("id", "BIGINT", not_null=True),
                ops.Column("first_seen_at", "TIMESTAMPTZ", not_null=True),
                ops.Column("team_id", "UUID", not_null=True),
                ops.Column("helpshift_id", "TEXT", not_null=True, default="''"),
                ops.Column("summary", "TEXT", not_null=True, default="''"),
                ops.Column("summary_embedding", "vector(8)"),
            ],
            composite_pk_columns=["id", "first_seen_at"],
        ),
        ops.AddIndex(
            ticket_table,
            f"idx_{ticket_table}_emb_hnsw",
            ["summary_embedding"],
            using="hnsw",
            opclasses=["vector_cosine_ops"],
        ),
        ops.CreateTable(
            issue_table,
            [
                ops.Column("id", "UUID", not_null=True, primary_key=True),
                ops.Column("team_id", "UUID", not_null=True),
                ops.Column("dedup_key", "TEXT", not_null=True),
                ops.Column("title", "TEXT", not_null=True, default="''"),
                ops.Column("summary", "TEXT", not_null=True, default="''"),
            ],
        ),
        ops.AddIndex(
            issue_table,
            f"idx_{issue_table}_team_dedup",
            ["team_id", "dedup_key"],
            unique=True,
        ),
        ops.CreateTable(
            alert_table,
            [
                ops.Column("id", "UUID", not_null=True, primary_key=True),
                ops.Column("team_id", "UUID", not_null=True),
                ops.Column("title", "TEXT", not_null=True, default="''"),
                ops.Column("ticket_ids", "UUID[]", not_null=True),
                ops.Column("slack_delivery", "JSONB", not_null=True),
            ],
        ),
        ops.EnableRLS(issue_table, force=True),
        ops.CreatePolicy(
            "team_isolation",
            issue_table,
            "team_id::text = current_setting('app.team_id', true)",
        ),
        ops.CreatePolicy(
            "platform_admin_bypass",
            issue_table,
            "current_setting('app.platform_admin', true) = 'true'",
        ),
        ops.CreateFunction(echo_fn, echo_body),
    ]
    await apply(pg_conn, _plan(f"ta_int_{suffix}", operations), dry_run=False)


async def _drop_schema(
    pg_conn: ferrum.connection.Connection,
    *,
    suffix: str,
    team_table: str,
    ticket_table: str,
    issue_table: str,
    alert_table: str,
    echo_fn: str,
) -> None:
    drop_ops = [
        ops.DropFunction(echo_fn),
        ops.DropPolicy("platform_admin_bypass", issue_table),
        ops.DropPolicy("team_isolation", issue_table),
        ops.DisableRLS(issue_table),
        ops.DropTable(alert_table),
        ops.DropTable(issue_table),
        ops.DropIndex(f"idx_{ticket_table}_emb_hnsw"),
        ops.DropTable(ticket_table),
        ops.DropTable(team_table),
    ]
    await apply(
        pg_conn,
        _plan(f"drop_ta_int_{suffix}", drop_ops),
        dry_run=False,
        confirm=True,
    )


async def _create_team(pg_conn: ferrum.connection.Connection, team_cls: type, *, name: str) -> Any:
    return await team_cls.objects.create(pg_conn, id=uuid.uuid4(), name=name)


@pytest_asyncio.fixture
async def compat_models(
    pg_conn: ferrum.connection.Connection,
    unique_suffix: str,
    require_native: None,
):
    Team, Ticket, Issue, Alert, echo_fn = _make_models(unique_suffix)
    team_table = Team.get_metadata().table_name
    ticket_table = Ticket.get_metadata().table_name
    issue_table = Issue.get_metadata().table_name
    alert_table = Alert.get_metadata().table_name

    await _apply_schema(
        pg_conn,
        suffix=unique_suffix,
        team_table=team_table,
        ticket_table=ticket_table,
        issue_table=issue_table,
        alert_table=alert_table,
        echo_fn=echo_fn,
    )
    try:
        yield {
            "Team": Team,
            "Ticket": Ticket,
            "Issue": Issue,
            "Alert": Alert,
            "echo_fn": echo_fn,
            "tables": (team_table, ticket_table, issue_table, alert_table),
        }
    finally:
        await _drop_schema(
            pg_conn,
            suffix=unique_suffix,
            team_table=team_table,
            ticket_table=ticket_table,
            issue_table=issue_table,
            alert_table=alert_table,
            echo_fn=echo_fn,
        )


@pytest.mark.integration
async def test_tenant_transaction_binds_team_guc(
    pg_conn: ferrum.connection.Connection,
    compat_models: dict[str, Any],
) -> None:
    Team = compat_models["Team"]
    Issue = compat_models["Issue"]

    team_a = await _create_team(pg_conn, Team, name="A")
    team_b = await _create_team(pg_conn, Team, name="B")
    async with tenant_transaction(pg_conn, team_a.id) as tx:
        await Issue.objects.create(
            tx,
            id=uuid.uuid4(),
            team_id=team_a.id,
            dedup_key="a1",
            title="for-a",
            summary="",
        )
    async with tenant_transaction(pg_conn, team_b.id) as tx:
        await Issue.objects.create(
            tx,
            id=uuid.uuid4(),
            team_id=team_b.id,
            dedup_key="b1",
            title="for-b",
            summary="",
        )

    async with tenant_transaction(pg_conn, team_a.id) as tx:
        guc = await current_setting(tx, "app.team_id")
        assert guc == str(team_a.id)
        # CI uses a PostgreSQL superuser, which bypasses FORCE RLS; filter by the
        # tenant id that was bound via GUC (production apps use a non-superuser role).
        visible = await Issue.objects.filter(team_id=team_a.id).all(tx)
        assert len(visible) == 1
        assert visible[0].dedup_key == "a1"


@pytest.mark.integration
async def test_ticket_composite_pk_create_and_get(
    pg_conn: ferrum.connection.Connection,
    compat_models: dict[str, Any],
) -> None:
    Team = compat_models["Team"]
    Ticket = compat_models["Ticket"]

    team = await _create_team(pg_conn, Team, name="team")
    seen = datetime(2024, 6, 1, 12, 0, 0, tzinfo=UTC)
    created = await Ticket.objects.create(
        pg_conn,
        id=42,
        first_seen_at=seen,
        team_id=team.id,
        helpshift_id="hs-1",
        summary="hello",
    )
    fetched = await Ticket.objects.filter(id=42, first_seen_at=seen).get(pg_conn)
    assert fetched.id == created.id
    assert fetched.first_seen_at == seen
    assert fetched.summary == "hello"


@pytest.mark.integration
async def test_issue_upsert_on_team_and_dedup_key(
    pg_conn: ferrum.connection.Connection,
    compat_models: dict[str, Any],
) -> None:
    Team = compat_models["Team"]
    Issue = compat_models["Issue"]

    team = await _create_team(pg_conn, Team, name="team")
    issue_id = uuid.uuid4()
    async with tenant_transaction(pg_conn, team.id) as tx:
        first = await Issue.objects.upsert(
            tx,
            conflict_fields=["team_id", "dedup_key"],
            update_fields=["title", "summary"],
            id=issue_id,
            team_id=team.id,
            dedup_key="dup-1",
            title="v1",
            summary="s1",
        )
        assert first is not None
        second = await Issue.objects.upsert(
            tx,
            conflict_fields=["team_id", "dedup_key"],
            update_fields=["title", "summary"],
            id=issue_id,
            team_id=team.id,
            dedup_key="dup-1",
            title="v2",
            summary="s2",
        )
    assert second is not None
    assert second.id == first.id
    assert second.title == "v2"


@pytest.mark.integration
async def test_vector_search_returns_score_column(
    pg_conn: ferrum.connection.Connection,
    compat_models: dict[str, Any],
) -> None:
    Team = compat_models["Team"]
    Ticket = compat_models["Ticket"]

    team = await _create_team(pg_conn, Team, name="team")
    seen = datetime(2024, 6, 2, tzinfo=UTC)
    ticket_table = Ticket.get_metadata().table_name
    pool = raw_pool(pg_conn)
    async with pool.acquire() as raw:
        await raw.execute(
            f'INSERT INTO "{ticket_table}" '
            f"(id, first_seen_at, team_id, helpshift_id, summary, summary_embedding) "
            f"VALUES ($1, $2, $3, '', 'near', $4::vector)",
            1,
            seen,
            team.id,
            "[1,0,0,0,0,0,0,0]",
        )
    rows = await vector_search(
        pg_conn,
        Ticket,
        "summary_embedding",
        [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
        limit=5,
    )
    assert rows
    assert "score" in rows[0]
    assert rows[0]["score"] is not None


@pytest.mark.integration
async def test_call_function_round_trip(
    pg_conn: ferrum.connection.Connection,
    compat_models: dict[str, Any],
) -> None:
    echo_fn = compat_models["echo_fn"]
    rows = await pg_conn.call_function(echo_fn, "ping")
    assert rows == [{"result": "ping"}]


@pytest.mark.integration
async def test_alert_uuid_array_and_jsonb_round_trip(
    pg_conn: ferrum.connection.Connection,
    compat_models: dict[str, Any],
) -> None:
    Team = compat_models["Team"]
    Alert = compat_models["Alert"]

    team = await _create_team(pg_conn, Team, name="team")
    tid = uuid.uuid4()
    alert_id = uuid.uuid4()
    payload = {"channel": "C123", "ok": True}
    alert_table = Alert.get_metadata().table_name
    pool = raw_pool(pg_conn)
    async with pool.acquire() as raw:
        await raw.execute(
            f'INSERT INTO "{alert_table}" (id, team_id, title, ticket_ids, slack_delivery) '
            f"VALUES ($1, $2, $3, $4::uuid[], $5::jsonb)",
            alert_id,
            team.id,
            "alert",
            [tid],
            json.dumps(payload),
        )
    fetched = await Alert.objects.filter(id=alert_id).get(pg_conn)
    assert list(fetched.ticket_ids) == [tid]
    delivery = fetched.slack_delivery
    if isinstance(delivery, str):
        delivery = json.loads(delivery)
    assert delivery == payload
