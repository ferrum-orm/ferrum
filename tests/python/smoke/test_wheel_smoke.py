"""Wheel smoke tests: verify the built native extension is functional.

These tests run post-install on the *built wheel* (not a dev ``maturin develop``
build) to prove the distributed artifact is correctly assembled.  Each test
exercises one slice of the ``ferrum._native`` contract:

- The extension module is importable and exposes the expected public symbols.
- ``compile_query`` accepts well-formed metadata + IR JSON and returns a result
  dict with ``sql_text``, ``bound_params``, ``fingerprint``, and ``operation``.
- ``hydrate_rows`` converts a JSON row payload to a Python list of dicts.
- A controlled bad IR raises ``_native.FerrumCompileError``, *not* a bare
  ``RuntimeError`` — the critical PyO3 boundary contract (ARCHITECTURE §6.2
  ERR-2).
- Compile error messages never echo submitted bound-parameter values (LOG-2).

Marked ``@pytest.mark.smoke``.  The CI ``smoke-install`` job runs:
``pytest tests/python/smoke -m smoke -v``

The tests also run safely during regular unit-test collection: if ``ferrum._native``
is absent the entire module is skipped via ``pytest.importorskip``.
"""

from __future__ import annotations

import json

import pytest

import ferrum

# Skip the whole module when the Rust extension has not been built / installed.
_native = pytest.importorskip(
    "ferrum._native",
    reason="ferrum._native not available — install the built wheel first",
)


class SmokeModel(ferrum.Model):
    """Minimal model used to drive the compiler and hydrator in smoke tests."""

    id: int = 0
    name: str = ""


@pytest.mark.smoke
class TestWheelSmoke:
    """Post-install smoke checks on the built ferrum wheel."""

    # ------------------------------------------------------------------
    # Module surface
    # ------------------------------------------------------------------

    def test_native_module_exposes_expected_symbols(self) -> None:
        """ferrum._native imports and exposes compile_query / hydrate_rows /
        all three exception classes."""
        assert callable(_native.compile_query), "compile_query must be callable"
        assert callable(_native.hydrate_rows), "hydrate_rows must be callable"
        assert issubclass(_native.FerrumCompileError, Exception)
        assert issubclass(_native.FerrumHydrationError, Exception)
        assert issubclass(_native.FerrumInternalError, Exception)

    # ------------------------------------------------------------------
    # compile_query happy path
    # ------------------------------------------------------------------

    def test_compile_query_returns_sql_dict(self) -> None:
        """compile_query with a well-formed IR produces a dict with sql_text.

        Validates the full result shape: sql_text contains SELECT, bound_params
        is a list, fingerprint is a non-empty string, and operation is 'select'.
        """
        metadata_json = SmokeModel.get_metadata().to_metadata_json()
        ir_json = SmokeModel.objects.filter(id=1).to_ir_json()

        result = _native.compile_query(metadata_json, ir_json)

        assert isinstance(result, dict), f"Expected dict, got {type(result)}"
        assert "sql_text" in result, "result must contain sql_text"
        assert "SELECT" in result["sql_text"].upper(), (
            f"sql_text should contain SELECT, got: {result['sql_text']!r}"
        )
        assert isinstance(result["bound_params"], list), "bound_params must be a list"
        assert isinstance(result["fingerprint"], str) and result["fingerprint"], (
            "fingerprint must be a non-empty string"
        )
        assert result["operation"] == "select", (
            f"operation must be 'select', got {result['operation']!r}"
        )

    # ------------------------------------------------------------------
    # hydrate_rows happy path
    # ------------------------------------------------------------------

    def test_hydrate_rows_returns_list_of_dicts(self) -> None:
        """hydrate_rows converts a JSON row payload to a list of Python dicts.

        Exercises the Rust hydrator (ferrum-core hydrate module) on the
        built wheel.  Uses a single row with id=42 and name='ferrum'.
        """
        metadata_json = SmokeModel.get_metadata().to_metadata_json()
        rows_json = json.dumps([{"id": 42, "name": "ferrum"}])

        rows = _native.hydrate_rows(metadata_json, rows_json)

        assert isinstance(rows, list), f"Expected list, got {type(rows)}"
        assert len(rows) == 1, f"Expected 1 row, got {len(rows)}"
        row = rows[0]
        assert isinstance(row, dict), f"Expected dict row, got {type(row)}"
        assert row["id"] == 42, f"id must be 42, got {row['id']!r}"
        assert row["name"] == "ferrum", f"name must be 'ferrum', got {row['name']!r}"

    # ------------------------------------------------------------------
    # Error boundary: controlled bad IR must raise FerrumCompileError
    # ------------------------------------------------------------------

    def test_bad_ir_version_raises_ferrum_compile_error(self) -> None:
        """A version-mismatched IR raises FerrumCompileError, not RuntimeError.

        This is the central PyO3 boundary contract (ARCHITECTURE §6.2 ERR-2):
        compile-time rejection must surface as a catchable FerrumCompileError,
        never as a bare RuntimeError carrying a raw Rust error message.
        """
        metadata_json = SmokeModel.get_metadata().to_metadata_json()
        ir = json.loads(SmokeModel.objects.to_ir_json())
        # Force a version the Rust core will reject before any SQL is emitted.
        ir["version"] = 9999
        bad_ir_json = json.dumps(ir)

        with pytest.raises(_native.FerrumCompileError):
            _native.compile_query(metadata_json, bad_ir_json)

    def test_compile_error_does_not_echo_bound_values(self) -> None:
        """FerrumCompileError messages must not echo submitted bound values (LOG-2).

        Injects a sentinel string as a bound value alongside an invalid operator
        to force a compile error.  The sentinel must not appear in the exception
        message regardless of how the Rust error is formatted.
        """
        metadata_json = SmokeModel.get_metadata().to_metadata_json()
        ir = json.loads(SmokeModel.objects.to_ir_json())
        sentinel = "WHEEL_SMOKE_SECRET_SENTINEL_VALUE_XYZ"
        ir["filters"].append(
            {
                "field": {"index": 0, "name": "id"},
                "operator": "bad_op_force_error",
                "value": {"type": "text", "value": sentinel},
            }
        )
        bad_ir_json = json.dumps(ir)

        with pytest.raises(Exception) as exc_info:
            _native.compile_query(metadata_json, bad_ir_json)

        assert sentinel not in str(exc_info.value), (
            f"Bound value sentinel {sentinel!r} must not appear in error message; "
            f"got: {exc_info.value!r}"
        )
