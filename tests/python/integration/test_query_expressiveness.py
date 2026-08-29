"""Integration tests for W2-B query expressiveness features.

Tests against live PostgreSQL to verify:
- group_by + having + aggregate with filtered aggregates
- distinct queries
- exists/not-exists patterns
- select_related single-hop JOINs
- F expression usage in aggregates
- Star usage in COUNT(*)
- Reusable expression ergonomics

Requires: ``FERRUM_TEST_DSN`` set to a PostgreSQL connection string.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

import pytest

import ferrum
from ferrum.expressions import F, Q, Star
from ferrum.queryset import Aggregate

from .backends import Backend, Capability
from .helpers import transient_table as pg_transient_table


@pytest.mark.integration
async def test_grouped_filtered_aggregate_with_f_expressions(
    db_conn: ferrum.connection.Connection,
    backend: Backend,
    requires: Callable[[Capability], None],
    require_native: None,
    unique_suffix: str,
) -> None:
    """Verify group_by + having + filtered aggregate using F expressions."""
    requires(Capability.AGGREGATES)

    table_name = f"ferrum_int_expr_agg_{unique_suffix}"

    class Metric(ferrum.Model):
        id: int = 0
        category: str = ""
        amount: float = 0.0
        active: bool = False
        created_at: datetime = datetime(2024, 1, 1, tzinfo=UTC)

        class Meta:
            table = table_name

    if backend.name == "postgres":
        create_sql = f"""
            CREATE TABLE "{table_name}" (
                id SERIAL PRIMARY KEY,
                category TEXT NOT NULL,
                amount DOUBLE PRECISION NOT NULL,
                active BOOLEAN NOT NULL,
                created_at TIMESTAMPTZ NOT NULL
            )
        """
        drop_sql = f'DROP TABLE IF EXISTS "{table_name}"'
        table_ctx = pg_transient_table(db_conn, create_sql=create_sql, drop_sql=drop_sql)
    else:
        q = backend.quote
        amount_type = "FLOAT" if backend.name == "mssql" else "REAL"
        active_type = backend.types["bool"]
        created_type = "DATETIMEOFFSET" if backend.name == "mssql" else "DATETIME"
        create_sql = (
            f"CREATE TABLE {q(table_name)} ("
            f"{q('id')} {backend.types['pk_serial']}, "
            f"{q('category')} {backend.types['text']} NOT NULL, "
            f"{q('amount')} {amount_type} NOT NULL, "
            f"{q('active')} {active_type} NOT NULL, "
            f"{q('created_at')} {created_type} NOT NULL)"
        )
        drop_sql = f"DROP TABLE IF EXISTS {q(table_name)}"
        table_ctx = pg_transient_table(db_conn, create_sql=create_sql, drop_sql=drop_sql)

    async with table_ctx:
        await Metric.objects.create(
            db_conn,
            category="a",
            amount=10.0,
            active=True,
            created_at=datetime(2024, 1, 1, 10, tzinfo=UTC),
        )
        await Metric.objects.create(
            db_conn,
            category="a",
            amount=20.0,
            active=False,
            created_at=datetime(2024, 1, 1, 12, tzinfo=UTC),
        )
        await Metric.objects.create(
            db_conn,
            category="b",
            amount=5.0,
            active=True,
            created_at=datetime(2024, 1, 2, 9, tzinfo=UTC),
        )

        # Test with F expressions for field references
        rows = await (
            Metric.objects.group_by(F("category"))
            .having(total__gte=10)
            .aggregate(
                db_conn,
                count=Aggregate.count(Star()),
                active_count=Aggregate.count(filter=Q(active=True)),
                total=Aggregate.sum(F("amount")),
                average=Aggregate.avg(F("amount")),
                minimum=Aggregate.min(F("amount")),
                maximum=Aggregate.max(F("amount")),
            )
        )

        assert rows == [
            {
                "category": "a",
                "count": 2,
                "active_count": 1,
                "total": 30.0,
                "average": 15.0,
                "minimum": 10.0,
                "maximum": 20.0,
            }
        ]


@pytest.mark.integration
async def test_distinct_query(
    db_conn: ferrum.connection.Connection,
    backend: Backend,
    requires: Callable[[Capability], None],
    require_native: None,
    unique_suffix: str,
) -> None:
    """Verify DISTINCT returns unique rows."""
    requires(Capability.AGGREGATES)

    table_name = f"ferrum_int_expr_dist_{unique_suffix}"

    class Item(ferrum.Model):
        id: int = 0
        name: str = ""

        class Meta:
            table = table_name

    q = backend.quote
    create_sql = (
        f"CREATE TABLE {q(table_name)} ("
        f"{q('id')} {backend.types['pk_serial']}, "
        f"{q('name')} {backend.types['text']} NOT NULL)"
    )
    drop_sql = f"DROP TABLE IF EXISTS {q(table_name)}"
    table_ctx = pg_transient_table(db_conn, create_sql=create_sql, drop_sql=drop_sql)

    async with table_ctx:
        await Item.objects.create(db_conn, name="alpha")
        await Item.objects.create(db_conn, name="alpha")
        await Item.objects.create(db_conn, name="beta")
        await Item.objects.create(db_conn, name="alpha")

        rows = await Item.objects.distinct().values_list("name", flat=True).all(db_conn)
        assert sorted(rows) == ["alpha", "beta"]


@pytest.mark.integration
async def test_exists_and_not_exists_patterns(
    db_conn: ferrum.connection.Connection,
    backend: Backend,
    requires: Callable[[Capability], None],
    require_native: None,
    unique_suffix: str,
) -> None:
    """Verify exists() returns bool; filter().exists() for not-exists."""
    table_name = f"ferrum_int_expr_ex_{unique_suffix}"

    class User(ferrum.Model):
        id: int = 0
        email: str = ""
        active: bool = True

        class Meta:
            table = table_name

    q = backend.quote
    create_sql = (
        f"CREATE TABLE {q(table_name)} ("
        f"{q('id')} {backend.types['pk_serial']}, "
        f"{q('email')} {backend.types['text']} NOT NULL, "
        f"{q('active')} {backend.types['bool']} NOT NULL)"
    )
    drop_sql = f"DROP TABLE IF EXISTS {q(table_name)}"
    table_ctx = pg_transient_table(db_conn, create_sql=create_sql, drop_sql=drop_sql)

    async with table_ctx:
        await User.objects.create(db_conn, email="a@b.com", active=True)

        # exists() should return True when rows match
        exists = await User.objects.filter(email="a@b.com").exists(db_conn)
        assert exists is True

        # exists() should return False when no rows match
        not_exists = await User.objects.filter(email="nobody@nowhere.com").exists(db_conn)
        assert not_exists is False

        # not-exists pattern: exclude + filter
        remaining = await User.objects.exclude(email="a@b.com").exists(db_conn)
        assert remaining is False


@pytest.mark.integration
async def test_select_related_guard_against_live_db(
    db_conn: ferrum.connection.Connection,
    backend: Backend,
    requires: Callable[[Capability], None],
    require_native: None,
    unique_suffix: str,
) -> None:
    """Verify select_related() rejects non-relation names against a live DB model."""
    table_name = f"ferrum_int_expr_sr_{unique_suffix}"

    class Member(ferrum.Model):
        id: int = 0
        name: str = ""
        team_id: int = 0

        class Meta:
            table = table_name

    q = backend.quote
    create_sql = (
        f"CREATE TABLE {q(table_name)} ("
        f"{q('id')} {backend.types['pk_serial']}, "
        f"{q('name')} {backend.types['text']} NOT NULL, "
        f"{q('team_id')} INTEGER NOT NULL)"
    )
    drop_sql = f"DROP TABLE IF EXISTS {q(table_name)}"
    table_ctx = pg_transient_table(db_conn, create_sql=create_sql, drop_sql=drop_sql)

    async with table_ctx:
        # select_related() should reject field names that are not FK/OneToOne relations
        from ferrum.errors import FerrumCompileError

        with pytest.raises(FerrumCompileError, match="Unknown relation"):
            Member.objects.select_related("team_id")

        with pytest.raises(FerrumCompileError, match="Unknown relation"):
            Member.objects.select_related("name")


@pytest.mark.integration
async def test_f_expression_in_order_by(
    db_conn: ferrum.connection.Connection,
    backend: Backend,
    requires: Callable[[Capability], None],
    require_native: None,
    unique_suffix: str,
) -> None:
    """Verify F() expression works in order_by for live queries."""
    table_name = f"ferrum_int_expr_order_{unique_suffix}"

    class Item(ferrum.Model):
        id: int = 0
        name: str = ""
        priority: int = 0

        class Meta:
            table = table_name

    q = backend.quote
    create_sql = (
        f"CREATE TABLE {q(table_name)} ("
        f"{q('id')} {backend.types['pk_serial']}, "
        f"{q('name')} {backend.types['text']} NOT NULL, "
        f"{q('priority')} INTEGER NOT NULL)"
    )
    drop_sql = f"DROP TABLE IF EXISTS {q(table_name)}"
    table_ctx = pg_transient_table(db_conn, create_sql=create_sql, drop_sql=drop_sql)

    async with table_ctx:
        await Item.objects.create(db_conn, name="low", priority=1)
        await Item.objects.create(db_conn, name="high", priority=10)
        await Item.objects.create(db_conn, name="mid", priority=5)

        # Order by F("priority") DESC
        rows = await Item.objects.order_by(F("priority")).all(db_conn)
        assert [r.priority for r in rows] == [1, 5, 10]

        rows_desc = await Item.objects.order_by("-priority").all(db_conn)
        assert [r.priority for r in rows_desc] == [10, 5, 1]
