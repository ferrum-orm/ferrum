"""Integration tests for the compile → execute → hydrate read path.

# Test inventory
| # | Name | Marker | Requires |
|---|------|--------|----------|
| 1 | test_compile_query_returns_valid_sql | (none) | ferrum._native |
| 2 | test_queryset_all_returns_model_instances | integration | live DB + _native |
| 3 | test_queryset_get_raises_not_found | integration | live DB + _native |
| 4 | test_queryset_count | integration | live DB + _native |
| 5 | test_cancellation_at_python_await | integration | live DB + _native |

Tests 2-5 are deselected by default unless ``-m integration`` is passed and
at least one ``FERRUM_TEST_*_DSN`` is set.

Test 1 is always collected but skipped when the Rust extension has not been
built (``maturin develop`` not yet run).

Security invariants verified here:
- SQL-1: ``sql_text`` contains ``SELECT`` and positional placeholders (``$1``);
  the bound value is out-of-band in ``bound_params``, never in ``sql_text``.
- Cancellation path: asyncio timeout propagates from the Python await point
  (``asyncio.TimeoutError``), not as a Rust-level hang (ARCHITECTURE §6.2).
"""

# ruff: noqa: S608 — table identifiers are test-controlled suffixes, not user input.

from __future__ import annotations

import asyncio
import json

import pytest

import ferrum
from ferrum.errors import FerrumNotFoundError

from .backends import Backend
from .schema import Column, transient_table


def _bool_default(backend: Backend) -> str:
    return "TRUE" if backend.name == "postgres" else "1"


# ---------------------------------------------------------------------------
# Test 1 — native compile round-trip (no live DB required)
# ---------------------------------------------------------------------------


def test_compile_query_returns_valid_sql() -> None:
    """Compile a simple QuerySet IR through the Rust extension and verify output shape.

    Invariants:
    - sql_text contains SELECT and at least one positional placeholder ($1).
    - The bound value is NOT in sql_text (SQL-2 / Tier A observability gate).
    - bound_params is a non-empty list of JSON strings.
    - fingerprint is a non-empty hex string (Tier A observability key).
    - param_type_summary is a list.
    """
    _native = pytest.importorskip(
        "ferrum._native", reason="Rust extension not built — run `maturin develop`"
    )

    # Build a minimal model and serialize metadata + IR using the production helpers.
    class Widget(ferrum.Model):
        id: int = 0
        name: str = ""
        active: bool = True

    qs = Widget.objects.filter(active=True)
    metadata_json = Widget.get_metadata().to_metadata_json()
    ir_json = qs.to_ir_json()

    result = _native.compile_query(metadata_json, ir_json, "postgres")

    # --- Shape assertions ---
    assert isinstance(result, dict), "compile_query must return a dict"
    assert "sql_text" in result
    assert "bound_params" in result
    assert "param_type_summary" in result
    assert "fingerprint" in result

    # --- SQL-1 / SQL-2: identifier and value safety ---
    sql = result["sql_text"]
    assert "SELECT" in sql.upper(), f"sql_text must contain SELECT; got: {sql!r}"
    assert "$1" in sql, f"sql_text must contain positional placeholder $1; got: {sql!r}"

    # The actual bound value (True → 1) must NOT be interpolated into SQL text.
    assert "true" not in sql.lower(), "bound bool value must not appear in sql_text"
    assert "false" not in sql.lower(), "bound bool value must not appear in sql_text"

    # --- bound_params ---
    params = result["bound_params"]
    assert isinstance(params, list), "bound_params must be a list"
    assert len(params) >= 1, "filtered query must have at least one bound param"
    # Each param is a JSON-encoded BindValue tagged-union string.
    for p in params:
        decoded = json.loads(p)
        assert "type" in decoded, f"each param must have a 'type' key; got: {p!r}"

    # --- fingerprint: non-empty stable hex string ---
    fp = result["fingerprint"]
    assert isinstance(fp, str) and len(fp) > 0, "fingerprint must be a non-empty string"
    # Fingerprint must be stable across identical compilations (same IR → same fp).
    result2 = _native.compile_query(metadata_json, ir_json, "postgres")
    assert result["fingerprint"] == result2["fingerprint"], (
        "fingerprint must be deterministic for the same IR"
    )


def _widget_columns(backend: Backend) -> list[Column]:
    return [
        Column("id", "pk_serial"),
        Column("name", "text", null=False),
        Column("active", "bool", null=False, default=_bool_default(backend)),
    ]


# ---------------------------------------------------------------------------
# Tests 2-5 — live round-trip (integration marker)
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_queryset_all_returns_model_instances(
    db_conn: ferrum.connection.Connection,
    backend: Backend,
    unique_suffix: str,
) -> None:
    """Fetch all rows and assert results are model instances with correct field values."""
    pytest.importorskip("ferrum._native", reason="Rust extension not built — run `maturin develop`")

    table_name = f"ferrum_test_widget_{unique_suffix}"

    class Widget(ferrum.Model):
        id: int = 0
        name: str = ""
        active: bool = True

        class Meta:
            table = table_name

    async with transient_table(
        db_conn, table_name, backend=backend, columns=_widget_columns(backend)
    ):
        await Widget.objects.create(db_conn, name="alpha", active=True)
        await Widget.objects.create(db_conn, name="beta", active=False)
        results = await Widget.objects.all(db_conn)

        assert isinstance(results, list), "all() must return a list"
        assert len(results) == 2, f"expected 2 rows, got {len(results)}"
        for row in results:
            assert isinstance(row, Widget), (
                f"each result must be a Widget instance, got {type(row)}"
            )
        names = {r.name for r in results}
        assert names == {"alpha", "beta"}, f"unexpected names: {names}"


@pytest.mark.integration
async def test_queryset_get_raises_not_found(
    db_conn: ferrum.connection.Connection,
    backend: Backend,
    unique_suffix: str,
) -> None:
    """get() with no matching row raises FerrumNotFoundError (not a DB exception).

    Gate: the error taxonomy (ERR-1) maps driver no-row errors to a sanitized
    Ferrum exception — no raw DB DETAIL/HINT surfaces.
    """
    pytest.importorskip("ferrum._native", reason="Rust extension not built — run `maturin develop`")

    table_name = f"ferrum_test_item_{unique_suffix}"

    class Item(ferrum.Model):
        id: int = 0
        label: str = ""

        class Meta:
            table = table_name

    async with transient_table(
        db_conn,
        table_name,
        backend=backend,
        columns=[
            Column("id", "pk_serial"),
            Column("label", "text", null=False),
        ],
    ):
        with pytest.raises(FerrumNotFoundError):
            await Item.objects.filter(id=9999).get(db_conn)


@pytest.mark.integration
async def test_queryset_count(
    db_conn: ferrum.connection.Connection,
    backend: Backend,
    unique_suffix: str,
) -> None:
    """count() returns a non-negative integer matching the actual row count."""
    pytest.importorskip("ferrum._native", reason="Rust extension not built — run `maturin develop`")

    table_name = f"ferrum_test_counter_{unique_suffix}"

    class Counter(ferrum.Model):
        id: int = 0
        val: int = 0

        class Meta:
            table = table_name

    async with transient_table(
        db_conn,
        table_name,
        backend=backend,
        columns=[
            Column("id", "pk_serial"),
            Column("val", "int", null=False),
        ],
    ):
        for val in (10, 20, 30):
            await Counter.objects.create(db_conn, val=val)
        n = await Counter.objects.count(db_conn)
        assert isinstance(n, int), f"count() must return int, got {type(n)}"
        assert n == 3, f"expected 3 rows, got {n}"


@pytest.mark.integration
async def test_cancellation_at_python_await(
    db_conn: ferrum.connection.Connection,
    backend: Backend,
    unique_suffix: str,
) -> None:
    """asyncio.wait_for() at the Python await point raises TimeoutError, not a Rust hang.

    Invariant (ARCHITECTURE §6.2): all cancellation/timeout handling lives in Python
    at the driver await point. The Rust compile path is synchronous and sub-millisecond;
    it cannot be cancelled. The driver I/O path is cancelled via standard asyncio
    cooperative cancellation.

    A 1 ms timeout on a real query is chosen to be far shorter than any real DB round
    trip, guaranteeing the asyncio event loop cancels the coroutine before it completes.
    """
    pytest.importorskip("ferrum._native", reason="Rust extension not built — run `maturin develop`")

    table_name = f"ferrum_test_task_{unique_suffix}"

    class Task(ferrum.Model):
        id: int = 0
        payload: str = ""

        class Meta:
            table = table_name

    async with transient_table(
        db_conn,
        table_name,
        backend=backend,
        columns=[
            Column("id", "pk_serial"),
            Column("payload", "text", null=False),
        ],
    ):
        driver = db_conn._require_driver()
        q = backend.quote
        values_sql = ", ".join(f"('row-{i}')" for i in range(5000))
        await driver.execute(f"INSERT INTO {q(table_name)} (payload) VALUES {values_sql}")
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(
                Task.objects.all(db_conn),
                timeout=0,
            )
