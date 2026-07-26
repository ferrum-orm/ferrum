from __future__ import annotations

from datetime import UTC, datetime

import pytest

import ferrum
from ferrum.expressions import Q
from ferrum.queryset import Aggregate

from .helpers import transient_table


@pytest.mark.integration
async def test_grouped_filtered_aggregate_and_compare_and_set(
    pg_conn: ferrum.connection.Connection,
    require_native: None,
    unique_suffix: str,
) -> None:
    table_name = f"ferrum_int_aggregate_{unique_suffix}"

    class Metric(ferrum.Model):
        id: int = 0
        category: str = ""
        amount: float = 0.0
        active: bool = False
        created_at: datetime = datetime(2024, 1, 1, tzinfo=UTC)

        class Meta:
            table = table_name

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

    async with transient_table(pg_conn, create_sql=create_sql, drop_sql=drop_sql):
        await Metric.objects.create(
            pg_conn,
            category="a",
            amount=10.0,
            active=True,
            created_at=datetime(2024, 1, 1, 10, tzinfo=UTC),
        )
        await Metric.objects.create(
            pg_conn,
            category="a",
            amount=20.0,
            active=False,
            created_at=datetime(2024, 1, 1, 12, tzinfo=UTC),
        )
        await Metric.objects.create(
            pg_conn,
            category="b",
            amount=5.0,
            active=True,
            created_at=datetime(2024, 1, 2, 9, tzinfo=UTC),
        )

        rows = await (
            Metric.objects.group_by("category")
            .having(total__gte=10)
            .aggregate(
                pg_conn,
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

        buckets = await Metric.objects.date_trunc("created_at", "day", alias="day").aggregate(
            pg_conn, count=Aggregate.count()
        )

        assert sorted(row["count"] for row in buckets) == [1, 2]

        claimed = await Metric.objects.filter(category="b", active=True).update_returning(
            pg_conn, active=False
        )
        lost_race = await Metric.objects.filter(category="b", active=True).update_returning(
            pg_conn, active=False
        )

        assert len(claimed) == 1
        assert claimed[0]["active"] is False
        assert lost_race == []
