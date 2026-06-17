"""PyO3 boundary tests: Rust panics and structured errors surface as Ferrum exceptions.

Invariants tested (ARCHITECTURE §6.2, ERR-2):
- ERR-2: A Rust panic caught by ``std::panic::catch_unwind`` must NOT propagate as a
  plain Python ``RuntimeError`` carrying a raw panic message, memory addresses, or
  local file paths. It must surface as ``_native.FerrumInternalError``.
- All non-panic compile failures (unknown field, bad IR structure, version mismatch,
  malformed JSON) must surface as ``_native.FerrumCompileError``, never plain
  ``RuntimeError``.
- The exception message must not contain bound parameter values, memory addresses,
  or local file paths (LOG-2).

Tests are skipped automatically when the Rust extension has not been built.
Run ``maturin develop`` to build it.
"""

from __future__ import annotations

import pytest

import ferrum


class TestRustPanicAndErrorBoundary:
    def test_rust_panic_surfaces_as_ferrum_internal_error(self) -> None:
        """Rust panics caught by ``catch_unwind`` must surface as ``FerrumInternalError``.

        The test injects structurally-malformed metadata JSON that is valid JSON but
        cannot be deserialized into ``ModelMetadata`` (missing required fields).  The
        expected result is ``FerrumCompileError`` (metadata deser failure is a compile-
        level error) or ``FerrumInternalError`` (panic, should not happen for this
        input).  A plain ``RuntimeError`` is never acceptable — it would mean a raw Rust
        panic message escaped the boundary without sanitisation (ERR-2).
        """
        _native = pytest.importorskip(
            "ferrum._native",
            reason="Rust extension not built — run `maturin develop`",
        )

        with pytest.raises(Exception) as exc_info:
            _native.compile_query("{}", "{}")

        exc = exc_info.value
        # Must be a Ferrum-typed exception from the extension, not a bare RuntimeError.
        assert isinstance(exc, (_native.FerrumCompileError, _native.FerrumInternalError)), (
            f"Expected FerrumCompileError or FerrumInternalError at the PyO3 boundary, "
            f"got {type(exc).__name__}: {exc}"
        )
        # Message must not carry a raw Rust panic payload (memory addresses, paths).
        msg = str(exc)
        assert "panicked at" not in msg, f"Raw Rust panic message escaped the boundary: {msg!r}"
        assert "RUST_BACKTRACE" not in msg, f"Backtrace hint escaped the boundary: {msg!r}"

    def test_ir_version_mismatch_surfaces_as_compile_error(self) -> None:
        """An IR with the wrong ``version`` field raises ``FerrumCompileError``, not RuntimeError.

        This exercises the ADR-002 IR version gate (IR must carry version == IR_VERSION).
        """
        _native = pytest.importorskip(
            "ferrum._native",
            reason="Rust extension not built — run `maturin develop`",
        )

        class Probe(ferrum.Model):
            id: int = 0
            label: str = ""

        import json

        metadata_json = Probe.get_metadata().to_metadata_json()
        ir = json.loads(Probe.objects.to_ir_json())
        # Force a version mismatch.
        ir["version"] = 999
        ir_json = json.dumps(ir)

        with pytest.raises(Exception) as exc_info:
            _native.compile_query(metadata_json, ir_json)

        assert isinstance(exc_info.value, _native.FerrumCompileError), (
            f"IR version mismatch must raise FerrumCompileError, "
            f"got {type(exc_info.value).__name__}"
        )

    def test_unknown_field_in_ir_surfaces_as_compile_error(self) -> None:
        """A filter referencing an out-of-range field index raises ``FerrumCompileError``.

        This exercises the Rust-side allowlist check (SQL-1).  The Python layer already
        validates field names before building the IR, but this test injects a bad index
        directly to verify the Rust guard is also present (Defense in Depth).
        """
        _native = pytest.importorskip(
            "ferrum._native",
            reason="Rust extension not built — run `maturin develop`",
        )

        class Probe(ferrum.Model):
            id: int = 0
            label: str = ""

        import json

        metadata_json = Probe.get_metadata().to_metadata_json()
        ir = json.loads(Probe.objects.to_ir_json())
        # Inject a filter with an out-of-range field index (bypassing Python guard).
        ir["filters"].append(
            {
                "field": {"index": 99, "name": "hacked"},
                "operator": "eq",
                "value": {"type": "text", "value": "injected"},
            }
        )
        ir_json = json.dumps(ir)

        with pytest.raises(Exception) as exc_info:
            _native.compile_query(metadata_json, ir_json)

        assert isinstance(exc_info.value, _native.FerrumCompileError), (
            f"Unknown field index must raise FerrumCompileError, "
            f"got {type(exc_info.value).__name__}"
        )

    def test_compile_error_message_does_not_contain_bound_values(self) -> None:
        """Compile error messages must never echo submitted bound parameter values (LOG-2).

        Injects a sentinel value into a valid IR and forces a compile error by using a
        bad operator.  The sentinel must not appear in the exception message.
        """
        _native = pytest.importorskip(
            "ferrum._native",
            reason="Rust extension not built — run `maturin develop`",
        )

        class Probe(ferrum.Model):
            id: int = 0
            label: str = ""

        import json

        metadata_json = Probe.get_metadata().to_metadata_json()
        ir = json.loads(Probe.objects.to_ir_json())
        sentinel = "TOP_SECRET_VALUE_12345"
        # Inject a filter with an unsupported operator; value carries the sentinel.
        ir["filters"].append(
            {
                "field": {"index": 0, "name": "id"},
                "operator": "invalid_op",
                "value": {"type": "text", "value": sentinel},
            }
        )
        ir_json = json.dumps(ir)

        with pytest.raises(Exception) as exc_info:
            _native.compile_query(metadata_json, ir_json)

        assert sentinel not in str(exc_info.value), (
            f"Bound value {sentinel!r} must not appear in error message"
        )

    def test_valid_compile_produces_no_exception(self) -> None:
        """Sanity: a well-formed IR compiles successfully with no exception."""
        _native = pytest.importorskip(
            "ferrum._native",
            reason="Rust extension not built — run `maturin develop`",
        )

        class Probe(ferrum.Model):
            id: int = 0
            label: str = ""

        metadata_json = Probe.get_metadata().to_metadata_json()
        ir_json = Probe.objects.filter(id=1).to_ir_json()

        result = _native.compile_query(metadata_json, ir_json)
        assert isinstance(result, dict)
        assert "sql_text" in result
        assert "SELECT" in result["sql_text"].upper()


# ---------------------------------------------------------------------------
# Hydration boundary: _native.hydrate_rows() error surface (ADR-003, ERR-2)
# ---------------------------------------------------------------------------


class TestHydrationBoundary:
    """Tests for ``_native.hydrate_rows()`` at the PyO3 boundary.

    All tests are skipped when the Rust extension has not been built.
    """

    def test_hydrate_rows_valid_returns_list(self) -> None:
        """Sanity: well-formed rows hydrate without exception and return a list."""
        _native = pytest.importorskip(
            "ferrum._native",
            reason="Rust extension not built — run `maturin develop`",
        )

        import json

        class Probe(ferrum.Model):
            id: int = 0
            label: str = ""

        metadata_json = Probe.get_metadata().to_metadata_json()
        rows_json = json.dumps([{"id": 1, "label": "hello"}])

        result = _native.hydrate_rows(metadata_json, rows_json)
        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0]["id"] == 1

    def test_hydrate_rows_null_required_column_raises_hydration_error(self) -> None:
        """ERR-1: a non-nullable column with NULL → FerrumHydrationError, not plain RuntimeError."""
        _native = pytest.importorskip(
            "ferrum._native",
            reason="Rust extension not built — run `maturin develop`",
        )

        import json

        class Probe(ferrum.Model):
            id: int = 0
            label: str = ""

        metadata_json = Probe.get_metadata().to_metadata_json()
        rows_json = json.dumps([{"id": 1, "label": None}])

        with pytest.raises(_native.FerrumHydrationError):
            _native.hydrate_rows(metadata_json, rows_json)

    def test_hydrate_rows_missing_required_column_raises_hydration_error(self) -> None:
        """A row missing a required non-nullable column → FerrumHydrationError."""
        _native = pytest.importorskip(
            "ferrum._native",
            reason="Rust extension not built — run `maturin develop`",
        )

        import json

        class Probe(ferrum.Model):
            id: int = 0
            label: str = ""

        metadata_json = Probe.get_metadata().to_metadata_json()
        rows_json = json.dumps([{"id": 1}])

        with pytest.raises(_native.FerrumHydrationError):
            _native.hydrate_rows(metadata_json, rows_json)

    def test_hydrate_rows_malformed_metadata_raises_hydration_error(self) -> None:
        """Structurally-malformed metadata JSON → FerrumHydrationError (deser failure)."""
        _native = pytest.importorskip(
            "ferrum._native",
            reason="Rust extension not built — run `maturin develop`",
        )

        import json

        with pytest.raises(_native.FerrumHydrationError):
            _native.hydrate_rows("{}", json.dumps([{"id": 1}]))

    def test_hydrate_rows_malformed_rows_json_raises_hydration_error(self) -> None:
        """Non-JSON rows argument → FerrumHydrationError (deser failure), not SyntaxError."""
        _native = pytest.importorskip(
            "ferrum._native",
            reason="Rust extension not built — run `maturin develop`",
        )

        class Probe(ferrum.Model):
            id: int = 0
            label: str = ""

        metadata_json = Probe.get_metadata().to_metadata_json()

        with pytest.raises(_native.FerrumHydrationError):
            _native.hydrate_rows(metadata_json, "not-json")

    def test_hydrate_rows_error_is_not_plain_runtime_error(self) -> None:
        """hydrate_rows errors must be typed FerrumHydrationError, not bare RuntimeError."""
        _native = pytest.importorskip(
            "ferrum._native",
            reason="Rust extension not built — run `maturin develop`",
        )

        import json

        class Probe(ferrum.Model):
            id: int = 0
            label: str = ""

        metadata_json = Probe.get_metadata().to_metadata_json()
        rows_json = json.dumps([{"id": 1, "label": None}])

        try:
            _native.hydrate_rows(metadata_json, rows_json)
        except Exception as exc:
            assert isinstance(exc, _native.FerrumHydrationError), (
                f"Expected FerrumHydrationError at boundary, got bare {type(exc).__name__}: {exc}"
            )

    def test_hydrate_rows_error_message_does_not_contain_row_values(self) -> None:
        """ERR-1: hydration error message must not echo row values (only model/column names)."""
        _native = pytest.importorskip(
            "ferrum._native",
            reason="Rust extension not built — run `maturin develop`",
        )

        import json

        class Probe(ferrum.Model):
            id: int = 0
            label: str = ""

        metadata_json = Probe.get_metadata().to_metadata_json()
        sentinel = "TOP_SECRET_ROW_VALUE_54321"
        rows_json = json.dumps([{"id": sentinel, "label": None}])

        with pytest.raises(_native.FerrumHydrationError) as exc_info:
            _native.hydrate_rows(metadata_json, rows_json)

        assert sentinel not in str(exc_info.value), (
            f"Row value {sentinel!r} must not appear in hydration error message"
        )


# ---------------------------------------------------------------------------
# Python-side _hydrate_rows wiring (ADR-003 live path)
# ---------------------------------------------------------------------------


class TestHydrateRowsPythonWiring:
    """Tests for the Python ``_hydrate_rows()`` function in ``ferrum.queryset``.

    These tests exercise the Rust-wiring path without a full asyncpg connection.
    When the native extension is available, the Rust NULL check runs before
    ``model_construct``. When it is not, the fallback path is tested instead.
    """

    def test_hydrate_rows_with_valid_data_constructs_models(self) -> None:
        """_hydrate_rows returns model instances from valid dict-like rows."""
        from ferrum.queryset import _hydrate_rows

        class Probe(ferrum.Model):
            id: int = 0
            label: str = ""

        rows = [{"id": 1, "label": "alpha"}, {"id": 2, "label": "beta"}]
        result = _hydrate_rows(Probe, rows)
        assert len(result) == 2
        assert result[0].id == 1
        assert result[1].label == "beta"

    def test_hydrate_rows_empty_returns_empty_list(self) -> None:
        """_hydrate_rows([]) → []."""
        from ferrum.queryset import _hydrate_rows

        class Probe(ferrum.Model):
            id: int = 0

        assert _hydrate_rows(Probe, []) == []

    def test_hydrate_rows_null_required_column_raises_ferrum_error(self) -> None:
        """NULL in non-nullable column raises FerrumHydrationError (native ext required)."""
        pytest.importorskip(
            "ferrum._native",
            reason="Rust extension not built — run `maturin develop`",
        )

        from ferrum.errors import FerrumHydrationError
        from ferrum.queryset import _hydrate_rows

        class Probe(ferrum.Model):
            id: int = 0
            label: str = ""

        rows = [{"id": 1, "label": None}]
        with pytest.raises(FerrumHydrationError):
            _hydrate_rows(Probe, rows)

    def test_hydrate_rows_null_nullable_column_is_ok(self) -> None:
        """A NULL in an Optional column is valid and must not raise."""
        from ferrum.queryset import _hydrate_rows

        class Probe(ferrum.Model):
            id: int = 0
            label: str | None = None

        rows = [{"id": 1, "label": None}]
        result = _hydrate_rows(Probe, rows)
        assert result[0].label is None

    def test_hydrate_rows_null_failure_emits_hydration_failure_hook(self) -> None:
        """On hydration failure, a Tier A 'hydration_failure' hook is dispatched."""
        pytest.importorskip(
            "ferrum._native",
            reason="Rust extension not built — run `maturin develop`",
        )

        from ferrum.errors import FerrumHydrationError
        from ferrum.hooks import HookPayload, clear_hooks, register_hook
        from ferrum.queryset import _hydrate_rows

        class Probe(ferrum.Model):
            id: int = 0
            label: str = ""

        received: list[HookPayload] = []
        register_hook("hydration_failure", received.append)
        try:
            with pytest.raises(FerrumHydrationError):
                _hydrate_rows(Probe, [{"id": 1, "label": None}], fingerprint="fp:Probe:select")
            assert len(received) == 1
            payload = received[0]
            assert payload["event"] == "hydration_failure"
            assert payload["model"] == "Probe"
            assert payload["status"] == "error"
        finally:
            clear_hooks()

    def test_hydrate_rows_preserves_native_python_types(self) -> None:
        """model_construct is called with native Python types, not JSON-serialized strings."""
        from datetime import datetime

        from ferrum.queryset import _hydrate_rows

        class Probe(ferrum.Model):
            id: int = 0
            created_at: datetime = datetime(2024, 1, 1)

        ts = datetime(2024, 6, 1, 12, 0, 0)
        rows = [{"id": 1, "created_at": ts}]
        result = _hydrate_rows(Probe, rows)
        assert isinstance(result[0].created_at, datetime), (
            "model_construct must receive native datetime, not a JSON string"
        )
        assert result[0].created_at == ts
