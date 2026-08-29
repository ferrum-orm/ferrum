from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

import pytest

import ferrum
from ferrum.expressions import Q
from ferrum.queryset import Aggregate

from .backends import Backend, Capability
from .helpers import transient_table as pg_transient_table


@pytest.mark.integration
async def test_grouped_filtered_aggregate_and_compare_and_set(
    db_conn: ferrum.connection.Connection,
    backend: Backend,
    requires: Callable[[Capability], None],
    require_native: None,
    unique_suffix: str,
) -> None:
    requires(Capability.AGGREGATES)

    table_name = f"ferrum_int_aggregate_{unique_suffix}"

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

        rows = await (
            Metric.objects.group_by("category")
            .having(total__gte=10)
            .aggregate(
                db_conn,
                count=Aggregate.count(),
                active_count=Aggregate.count(filter=Q(active=True)),
                total=Aggregate.sum("amount"),
                average=Aggregate.avg("amount"),
                minimum=Aggregate.min("amount"),
                maximum=Aggregate.max("amount"),
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

        if backend.name == "postgres":
            buckets = await Metric.objects.date_trunc("created_at", "day", alias="day").aggregate(
                db_conn, count=Aggregate.count()
            )
            assert sorted(row["count"] for row in buckets) == [1, 2]

        if Capability.RETURNING in backend.capabilities:
            claimed = await Metric.objects.filter(category="b", active=True).update_returning(
                db_conn, active=False
            )
            lost_race = await Metric.objects.filter(category="b", active=True).update_returning(
                db_conn, active=False
            )

            assert len(claimed) == 1
            assert claimed[0]["active"] is False
            assert lost_race == []
