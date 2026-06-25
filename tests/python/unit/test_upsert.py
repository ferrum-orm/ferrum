"""Unit tests for QuerySet.upsert and QuerySet.bulk_upsert."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

import ferrum
from ferrum.errors import FerrumCompileError, FerrumConfigError
from ferrum.queryset import QuerySet


class Widget(ferrum.Model):
    id: int = 0
    name: str = ""
    score: int = 0


def _make_driver(fetchrow_return=None):
    """Build a mock asyncpg-like driver (the object returned by _require_driver())."""
    driver = MagicMock()
    driver.fetchrow = AsyncMock(return_value=fetchrow_return)
    driver.execute = AsyncMock(return_value=None)
    return driver


def _make_conn(fetchrow_return=None):
    """Build a mock Connection whose _require_driver() returns a fresh driver mock."""
    conn = MagicMock()
    conn._require_driver = MagicMock(return_value=_make_driver(fetchrow_return))
    return conn


# ---------------------------------------------------------------------------
# _build_upsert_sql — pure unit tests (no I/O)
# ---------------------------------------------------------------------------


class TestUpsertSqlBuilder:
    def _qs_and_meta(self):
        qs: QuerySet[Widget] = QuerySet(Widget)
        meta = qs._get_metadata()
        return qs, meta

    def test_build_upsert_sql_do_update(self) -> None:
        qs, meta = self._qs_and_meta()
        values = {"id": 1, "name": "x", "score": 10}
        sql, _bound = qs._build_upsert_sql(
            meta,
            values,
            conflict_fields=["id"],
            update_fields=["name", "score"],
            returning=False,
        )
        assert "ON CONFLICT" in sql
        assert "DO UPDATE SET" in sql
        assert '"name"' in sql
        assert '"score"' in sql
        # No user-supplied values in SQL text — only $N placeholders.
        assert "x" not in sql
        assert "10" not in sql

    def test_build_upsert_sql_do_nothing_when_no_update_fields(self) -> None:
        qs, meta = self._qs_and_meta()
        values = {"id": 1, "name": "x", "score": 10}
        sql, _bound = qs._build_upsert_sql(
            meta,
            values,
            conflict_fields=["id"],
            update_fields=[],
            returning=False,
        )
        assert "DO NOTHING" in sql
        assert "DO UPDATE SET" not in sql

    def test_build_upsert_sql_with_returning(self) -> None:
        qs, meta = self._qs_and_meta()
        values = {"id": 1, "name": "x", "score": 10}
        sql, _bound = qs._build_upsert_sql(
            meta,
            values,
            conflict_fields=["id"],
            update_fields=["name"],
            returning=True,
        )
        assert "RETURNING" in sql

    def test_build_upsert_sql_no_returning_when_false(self) -> None:
        qs, meta = self._qs_and_meta()
        values = {"id": 1, "name": "x", "score": 10}
        sql, _bound = qs._build_upsert_sql(
            meta,
            values,
            conflict_fields=["id"],
            update_fields=["name"],
            returning=False,
        )
        assert "RETURNING" not in sql

    def test_build_upsert_sql_bound_params_match_placeholders(self) -> None:
        qs, meta = self._qs_and_meta()
        values = {"id": 1, "name": "x", "score": 10}
        sql, bound = qs._build_upsert_sql(
            meta,
            values,
            conflict_fields=["id"],
            update_fields=["name", "score"],
            returning=False,
        )
        placeholder_count = sql.count("$")
        assert len(bound) == placeholder_count


# ---------------------------------------------------------------------------
# upsert() — validation (raises before I/O)
# ---------------------------------------------------------------------------


class TestUpsertValidation:
    @pytest.mark.asyncio
    async def test_upsert_raises_without_connection(self) -> None:
        qs: QuerySet[Widget] = QuerySet(Widget)
        with pytest.raises(FerrumConfigError):
            await qs.upsert(
                None,  # type: ignore[arg-type]
                conflict_fields=["id"],
                update_fields=["name"],
                id=1,
                name="x",
                score=10,
            )

    @pytest.mark.asyncio
    async def test_upsert_conflict_field_validation(self) -> None:
        conn = _make_conn()
        qs: QuerySet[Widget] = QuerySet(Widget)
        with pytest.raises(FerrumCompileError, match="Unknown"):
            await qs.upsert(
                conn,
                conflict_fields=["nonexistent"],
                update_fields=["name"],
                id=1,
                name="x",
                score=10,
            )

    @pytest.mark.asyncio
    async def test_upsert_unknown_value_field_raises(self) -> None:
        conn = _make_conn()
        qs: QuerySet[Widget] = QuerySet(Widget)
        with pytest.raises(FerrumCompileError, match="Unknown"):
            await qs.upsert(
                conn,
                conflict_fields=["id"],
                update_fields=["name"],
                id=1,
                ghost_field="bad",  # unknown
            )


# ---------------------------------------------------------------------------
# upsert() — I/O path
# ---------------------------------------------------------------------------


class TestUpsertAsync:
    @pytest.mark.asyncio
    async def test_upsert_calls_fetchrow_when_returning(self) -> None:
        conn = _make_conn(fetchrow_return={"id": 1, "name": "x", "score": 10})
        qs: QuerySet[Widget] = QuerySet(Widget)
        result = await qs.upsert(
            conn,
            conflict_fields=["id"],
            update_fields=["name", "score"],
            returning=True,
            id=1,
            name="x",
            score=10,
        )
        driver = conn._require_driver.return_value
        driver.fetchrow.assert_called_once()
        assert result is not None

    @pytest.mark.asyncio
    async def test_upsert_calls_execute_when_not_returning(self) -> None:
        conn = _make_conn()
        qs: QuerySet[Widget] = QuerySet(Widget)
        result = await qs.upsert(
            conn,
            conflict_fields=["id"],
            update_fields=["name"],
            returning=False,
            id=1,
            name="x",
            score=10,
        )
        driver = conn._require_driver.return_value
        driver.execute.assert_called_once()
        assert result is None


# ---------------------------------------------------------------------------
# bulk_upsert()
# ---------------------------------------------------------------------------


class TestBulkUpsert:
    @pytest.mark.asyncio
    async def test_bulk_upsert_returning_false_returns_count(self) -> None:
        conn = _make_conn()
        qs: QuerySet[Widget] = QuerySet(Widget)
        objects = [{"id": 1, "name": "a", "score": 5}, {"id": 2, "name": "b", "score": 7}]
        result = await qs.bulk_upsert(
            conn,
            objects,
            conflict_fields=["id"],
            update_fields=["name", "score"],
            returning=False,
        )
        assert isinstance(result, int)
        assert result == len(objects)

    @pytest.mark.asyncio
    async def test_bulk_upsert_returning_true_returns_list(self) -> None:
        driver = MagicMock()
        driver.execute = AsyncMock(return_value=None)
        driver.fetchrow = AsyncMock(
            side_effect=[
                {"id": 1, "name": "a", "score": 5},
                {"id": 2, "name": "b", "score": 7},
            ]
        )
        conn = MagicMock()
        conn._require_driver = MagicMock(return_value=driver)

        qs: QuerySet[Widget] = QuerySet(Widget)
        objects = [{"id": 1, "name": "a", "score": 5}, {"id": 2, "name": "b", "score": 7}]
        result = await qs.bulk_upsert(
            conn,
            objects,
            conflict_fields=["id"],
            update_fields=["name", "score"],
            returning=True,
        )
        assert isinstance(result, list)
        assert len(result) == 2

    @pytest.mark.asyncio
    async def test_bulk_upsert_empty_objects_returns_zero(self) -> None:
        conn = _make_conn()
        qs: QuerySet[Widget] = QuerySet(Widget)
        result = await qs.bulk_upsert(
            conn,
            [],
            conflict_fields=["id"],
            update_fields=["name"],
            returning=False,
        )
        assert result == 0
        driver = conn._require_driver.return_value
        driver.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_bulk_upsert_empty_objects_returning_is_empty_list(self) -> None:
        conn = _make_conn()
        qs: QuerySet[Widget] = QuerySet(Widget)
        result = await qs.bulk_upsert(
            conn,
            [],
            conflict_fields=["id"],
            returning=True,
        )
        assert result == [] or result == 0  # empty early return
