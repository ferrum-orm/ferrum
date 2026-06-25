"""Unit tests for pgvector vector_search helper and codec hardening."""

from __future__ import annotations

from typing import Annotated
from unittest.mock import AsyncMock, MagicMock

import pytest

import ferrum
from ferrum.errors import FerrumCompileError, FerrumConfigError
from ferrum.ext.pgvector import (
    _METRIC_OPS,
    _encode_vector,
    register_vector_codecs,
    vector_search,
)

# ---------------------------------------------------------------------------
# Test model
# ---------------------------------------------------------------------------


class Article(ferrum.Model):
    id: int = 0
    title: str = ""
    embedding: Annotated[ferrum.Vector, ferrum.Field(vector_dimensions=4)]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_conn(*, dialect: str = "postgres") -> MagicMock:
    conn = MagicMock()
    conn.dialect = dialect
    return conn


def _make_pg_conn(fetch_return: list | None = None) -> MagicMock:
    conn = _make_conn(dialect="postgres")
    driver = MagicMock()
    driver.fetch = AsyncMock(return_value=fetch_return or [])
    conn._require_driver.return_value = driver
    return conn


# ---------------------------------------------------------------------------
# _encode_vector
# ---------------------------------------------------------------------------


class TestEncodeVector:
    def test_encodes_floats(self) -> None:
        assert _encode_vector([1.0, 2.0, 3.0]) == "[1.0,2.0,3.0]"

    def test_encodes_empty(self) -> None:
        assert _encode_vector([]) == "[]"


# ---------------------------------------------------------------------------
# vector_search — validation errors
# ---------------------------------------------------------------------------


class TestVectorSearchValidation:
    @pytest.mark.asyncio
    async def test_non_postgres_conn_raises_config_error(self) -> None:
        conn = _make_conn(dialect="mysql")
        with pytest.raises(FerrumConfigError, match="PostgreSQL"):
            await vector_search(conn, Article, "embedding", [0.1, 0.2, 0.3, 0.4])

    @pytest.mark.asyncio
    async def test_unknown_metric_raises_compile_error(self) -> None:
        conn = _make_pg_conn()
        with pytest.raises(FerrumCompileError, match="Unknown vector metric"):
            await vector_search(conn, Article, "embedding", [0.1, 0.2, 0.3, 0.4], metric="dot")

    @pytest.mark.asyncio
    async def test_unknown_field_raises_compile_error(self) -> None:
        conn = _make_pg_conn()
        with pytest.raises(FerrumCompileError, match="Unknown field"):
            await vector_search(conn, Article, "nonexistent_field", [0.1, 0.2, 0.3, 0.4])

    @pytest.mark.asyncio
    async def test_non_vector_field_raises_compile_error(self) -> None:
        conn = _make_pg_conn()
        with pytest.raises(FerrumCompileError, match="not a vector field"):
            await vector_search(conn, Article, "title", [0.1, 0.2, 0.3, 0.4])

    @pytest.mark.asyncio
    async def test_unknown_filter_key_raises_compile_error(self) -> None:
        conn = _make_pg_conn()
        with pytest.raises(FerrumCompileError, match="Unknown filter field"):
            await vector_search(
                conn,
                Article,
                "embedding",
                [0.1, 0.2, 0.3, 0.4],
                filters={"ghost_col": 1},
            )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("metric", list(_METRIC_OPS))
    async def test_valid_metrics_do_not_raise(self, metric: str) -> None:
        conn = _make_pg_conn(fetch_return=[])
        # Should not raise any exception
        await vector_search(conn, Article, "embedding", [0.1, 0.2, 0.3, 0.4], metric=metric)


# ---------------------------------------------------------------------------
# vector_search — SQL shape and bound params
# ---------------------------------------------------------------------------


class TestVectorSearchSql:
    """Verify the SQL text and bound-param list for each metric."""

    async def _run(self, metric: str, filters: dict | None = None) -> tuple[str, list]:
        captured: dict[str, object] = {}

        async def fake_fetch(sql: str, *params: object) -> list:
            captured["sql"] = sql
            captured["params"] = list(params)
            return []

        driver = MagicMock()
        driver.fetch = fake_fetch
        conn = MagicMock()
        conn.dialect = "postgres"
        conn._require_driver.return_value = driver

        await vector_search(
            conn,
            Article,
            "embedding",
            [0.1, 0.2, 0.3, 0.4],
            metric=metric,
            limit=5,
            filters=filters,
        )
        return str(captured["sql"]), list(captured["params"])  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_cosine_sql_uses_arrow_op(self) -> None:
        sql, _ = await self._run("cosine")
        assert "<=>" in sql
        assert "1 - " in sql

    @pytest.mark.asyncio
    async def test_l2_sql_uses_arrow_op(self) -> None:
        sql, _ = await self._run("l2")
        assert "<->" in sql
        assert "1 / (1 +" in sql

    @pytest.mark.asyncio
    async def test_inner_product_sql_uses_hash_op(self) -> None:
        sql, _ = await self._run("inner_product")
        assert "<#>" in sql

    @pytest.mark.asyncio
    async def test_bound_params_order(self) -> None:
        _, params = await self._run("cosine")
        assert params[0] == "[0.1,0.2,0.3,0.4]"
        assert params[1] == 5

    @pytest.mark.asyncio
    async def test_limit_is_second_param(self) -> None:
        sql, _ = await self._run("cosine")
        assert "$2" in sql  # LIMIT $2

    @pytest.mark.asyncio
    async def test_filter_adds_where_clause_and_param(self) -> None:
        sql, params = await self._run("cosine", filters={"id": 42})
        assert "$3" in sql
        assert params[2] == 42
        assert '"id" = $3' in sql

    @pytest.mark.asyncio
    async def test_vector_field_is_not_null_guard(self) -> None:
        sql, _ = await self._run("cosine")
        assert "IS NOT NULL" in sql

    @pytest.mark.asyncio
    async def test_table_name_from_metadata(self) -> None:
        sql, _ = await self._run("cosine")
        assert '"article"' in sql

    @pytest.mark.asyncio
    async def test_score_alias_default(self) -> None:
        sql, _ = await self._run("cosine")
        assert '"score"' in sql

    @pytest.mark.asyncio
    async def test_custom_score_alias(self) -> None:
        captured: dict = {}

        async def fake_fetch(sql: str, *params: object) -> list:
            captured["sql"] = sql
            return []

        driver = MagicMock()
        driver.fetch = fake_fetch
        conn = MagicMock()
        conn.dialect = "postgres"
        conn._require_driver.return_value = driver

        await vector_search(
            conn,
            Article,
            "embedding",
            [0.1, 0.2, 0.3, 0.4],
            score_alias="similarity",
        )
        assert '"similarity"' in captured["sql"]


# ---------------------------------------------------------------------------
# Score formula correctness
# ---------------------------------------------------------------------------


class TestScoreFormulas:
    """Verify the score expression templates are mathematically correct."""

    def _score_expr(self, metric: str) -> str:
        _, tmpl = _METRIC_OPS[metric]
        return tmpl.format(field='"embedding"')

    def test_cosine_formula(self) -> None:
        assert self._score_expr("cosine") == '1 - ("embedding" <=> $1::vector)'

    def test_l2_formula(self) -> None:
        assert self._score_expr("l2") == '1 / (1 + ("embedding" <-> $1::vector))'

    def test_inner_product_formula(self) -> None:
        assert self._score_expr("inner_product") == '-("embedding" <#> $1::vector)'


# ---------------------------------------------------------------------------
# register_vector_codecs — hardening
# ---------------------------------------------------------------------------


class TestRegisterVectorCodecsHardening:
    @pytest.mark.asyncio
    async def test_non_postgres_raises_config_error(self) -> None:
        conn = _make_conn(dialect="sqlite")
        with pytest.raises(FerrumConfigError, match="PostgreSQL"):
            await register_vector_codecs(conn)

    @pytest.mark.asyncio
    async def test_closed_pool_raises_config_error(self) -> None:
        conn = _make_conn(dialect="postgres")
        driver = MagicMock()
        driver._pool = None
        driver._inner = None
        conn._require_driver.return_value = driver
        with pytest.raises(FerrumConfigError, match="pool is not open"):
            await register_vector_codecs(conn)

    @pytest.mark.asyncio
    async def test_duplicate_object_error_is_swallowed(self) -> None:
        """DuplicateObjectError from concurrent CREATE EXTENSION is benign."""

        class DuplicateObjectError(Exception):
            pass

        pool = MagicMock()
        pool.execute = AsyncMock(side_effect=DuplicateObjectError())
        pool.set_type_codec = AsyncMock()

        driver = MagicMock()
        driver._pool = pool

        conn = _make_conn(dialect="postgres")
        conn._require_driver.return_value = driver

        # Should not propagate — swallowed as idempotent
        await register_vector_codecs(conn)

    @pytest.mark.asyncio
    async def test_codec_already_registered_is_swallowed(self) -> None:
        """Re-registration (InvalidStateError) on an already-configured pool is benign."""

        class InvalidStateError(Exception):
            pass

        pool = MagicMock()
        pool.execute = AsyncMock()
        pool.set_type_codec = AsyncMock(side_effect=InvalidStateError())

        driver = MagicMock()
        driver._pool = pool

        conn = _make_conn(dialect="postgres")
        conn._require_driver.return_value = driver

        await register_vector_codecs(conn)

    @pytest.mark.asyncio
    async def test_unknown_extension_error_propagates(self) -> None:
        """Unexpected errors during CREATE EXTENSION must propagate."""

        class _UnexpectedError(Exception):
            pass

        pool = MagicMock()
        pool.execute = AsyncMock(side_effect=_UnexpectedError("disk full"))
        pool.set_type_codec = AsyncMock()

        driver = MagicMock()
        driver._pool = pool

        conn = _make_conn(dialect="postgres")
        conn._require_driver.return_value = driver

        with pytest.raises(_UnexpectedError):
            await register_vector_codecs(conn)

    @pytest.mark.asyncio
    async def test_timeout_parameter_is_accepted(self) -> None:
        pool = MagicMock()
        pool.execute = AsyncMock()
        pool.set_type_codec = AsyncMock()

        driver = MagicMock()
        driver._pool = pool

        conn = _make_conn(dialect="postgres")
        conn._require_driver.return_value = driver

        await register_vector_codecs(conn, timeout=10.0)
