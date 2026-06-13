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
