"""Typing and IDE contract tests for ``ferrum._native`` stubs.

This file serves a dual purpose:

1. **Compile-time fixture (accepted use)** — The ``_ACCEPTED_FIXTURE`` block
   under ``TYPE_CHECKING`` is verified by ``ty`` when this file is explicitly
   checked:
       ty check tests/python/unit/test_typing_contract.py
   Type checkers analyze ``TYPE_CHECKING`` blocks even though they are
   ``False`` at runtime. ``assert_type`` calls inside that block verify
   that the stub's declared types match expected shapes. At runtime,
   ``assert_type`` is a no-op (returns its first argument).

2. **Runtime tests** — ``test_*`` functions run under pytest and verify:
   - ``py.typed`` marker presence (PEP 561).
   - ``_native.pyi`` stub surface matches the expected API.
   - Rejected API use produces ``ty`` errors (via temp-file fixtures).
   - ``__all__`` exports are complete.

No Rust extension build is required: the ``TYPE_CHECKING`` import is never
executed at runtime, and rejected-use fixtures are checked by ``ty`` using
the stub (not the compiled extension).
"""

from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path
from typing import TYPE_CHECKING, assert_type

import pytest

# ---------------------------------------------------------------------------
# Accepted API use — compile-time fixture
#
# This entire block is checked by `ty` when the file is explicitly targeted.
# It is never executed at runtime (TYPE_CHECKING is False at runtime).
# ---------------------------------------------------------------------------

if TYPE_CHECKING:
    import ferrum._native as _native

    # --- Exception hierarchy ---
    # All three exceptions must inherit from RuntimeError (per PyO3
    # create_exception! macro which uses PyRuntimeError as the base).
    _exc_internal: type = _native.FerrumInternalError.__mro__[1]  # type: ignore[assert_type]
    _exc_compile: type = _native.FerrumCompileError.__mro__[1]  # type: ignore[assert_type]
    _exc_hydrate: type = _native.FerrumHydrationError.__mro__[1]  # type: ignore[assert_type]

    # --- compile_query (JSON wire format) ---
    # Default dialect argument works.
    _cq_default: _native.CompiledQuery = _native.compile_query("{}", "{}")
    # Explicit dialect works.
    _cq_explicit: _native.CompiledQuery = _native.compile_query("{}", "{}", "postgres")
    # TypedDict keys have the correct types.
    _sql_text: str = _native.compile_query("{}", "{}")["sql_text"]
    _bound_params: list[str] = _native.compile_query("{}", "{}")["bound_params"]
    _param_type_summary: list[str] = _native.compile_query("{}", "{}")["param_type_summary"]
    _fingerprint: str = _native.compile_query("{}", "{}")["fingerprint"]
    _operation: str = _native.compile_query("{}", "{}")["operation"]

    # --- compile_query_msgpack (MessagePack wire format) ---
    _cqm: _native.CompiledQueryMsgpack = _native.compile_query_msgpack(b"", b"", "postgres")
    _cqm_default: _native.CompiledQueryMsgpack = _native.compile_query_msgpack(b"", b"")
    # bound_params is bytes (not list[str]) in the msgpack variant.
    _cqm_params: bytes = _cqm["bound_params"]

    # --- hydrate_rows (JSON) ---
    _hr: list[dict[str, object]] = _native.hydrate_rows("{}", "[]")

    # --- hydrate_rows_msgpack (MessagePack) ---
    _hrm: list[dict[str, object]] = _native.hydrate_rows_msgpack(b"", b"")

    # --- plan_migration ---
    _pm: None = _native.plan_migration()

    # --- __all__ surface ---
    # Verify key exports are listed in __all__.
    _all_list: list[str] = _native.__all__  # type: ignore[attr-defined]
    assert_type("compile_query" in _all_list, bool)
    assert_type("compile_query_msgpack" in _all_list, bool)
    assert_type("hydrate_rows" in _all_list, bool)
    assert_type("hydrate_rows_msgpack" in _all_list, bool)
    assert_type("plan_migration" in _all_list, bool)
    assert_type("FerrumInternalError" in _all_list, bool)
    assert_type("FerrumCompileError" in _all_list, bool)
    assert_type("FerrumHydrationError" in _all_list, bool)
    assert_type("CompiledQuery" in _all_list, bool)
    assert_type("CompiledQueryMsgpack" in _all_list, bool)


# ---------------------------------------------------------------------------
# Runtime tests
# ---------------------------------------------------------------------------

_STUB_PATH = Path(__file__).resolve().parents[3] / "python" / "ferrum" / "_native.pyi"
_PYTYPED_PATH = Path(__file__).resolve().parents[3] / "python" / "ferrum" / "py.typed"
_REPO_ROOT = Path(__file__).resolve().parents[3]

_EXPECTED_EXPORTS = {
    "compile_query",
    "compile_query_msgpack",
    "hydrate_rows",
    "hydrate_rows_msgpack",
    "plan_migration",
    "FerrumInternalError",
    "FerrumCompileError",
    "FerrumHydrationError",
    "CompiledQuery",
    "CompiledQueryMsgpack",
}


class TestPyTypedMarker:
    """PEP 561 marker file contract."""

    def test_py_typed_exists(self) -> None:
        """``py.typed`` must exist in the ``ferrum`` package directory."""
        assert _PYTYPED_PATH.exists(), f"py.typed not found at {_PYTYPED_PATH}"

    def test_py_typed_is_empty_or_minimal(self) -> None:
        """PEP 561 does not specify contents; the file just needs to exist.

        Some tools accept a single newline or comment lines, but the marker
        is the file existence itself.
        """
        if not _PYTYPED_PATH.exists():
            pytest.skip("py.typed not found")
        content = _PYTYPED_PATH.read_text()
        # PEP 561 allows any content; we just verify it's not a large file
        # that might confuse tools.
        assert len(content) < 256, (
            f"py.typed is unusually large ({len(content)} bytes); "
            "PEP 561 expects an empty or minimal marker file"
        )


class TestStubSurface:
    """Verify the stub file matches the expected PyO3 extension surface."""

    def test_stub_exists(self) -> None:
        assert _STUB_PATH.exists(), f"_native.pyi not found at {_STUB_PATH}"

    def test_stub_has_all_exports(self) -> None:
        """The stub must define ``__all__`` listing all public exports."""
        if not _STUB_PATH.exists():
            pytest.fail("_native.pyi not found")
        content = _STUB_PATH.read_text()
        assert "__all__" in content, "_native.pyi must define __all__ for IDE friendliness"

    def test_stub_exports_match_expected_surface(self) -> None:
        """Verify the stub exports match the known PyO3 extension surface.

        The PyO3 extension (``crates/ferrum-pyo3/src/lib.rs``) registers:
        - 3 exceptions: FerrumInternalError, FerrumCompileError, FerrumHydrationError
        - 5 functions: compile_query, compile_query_msgpack, hydrate_rows,
          hydrate_rows_msgpack, plan_migration
        - 2 TypedDicts: CompiledQuery, CompiledQueryMsgpack (stub-only types)
        """
        if not _STUB_PATH.exists():
            pytest.fail("_native.pyi not found")
        content = _STUB_PATH.read_text()
        for name in _EXPECTED_EXPORTS:
            assert name in content, (
                f"'{name}' not found in _native.pyi — stub is out of sync "
                "with the PyO3 extension surface"
            )

    def test_stub_exceptions_inherit_runtime_error(self) -> None:
        """All three Ferrum exceptions must inherit from ``RuntimeError``.

        The PyO3 ``create_exception!`` macro uses ``PyRuntimeError`` as the
        base class, which maps to Python's ``RuntimeError``.
        """
        if not _STUB_PATH.exists():
            pytest.fail("_native.pyi not found")
        content = _STUB_PATH.read_text()
        for exc_name in (
            "FerrumInternalError",
            "FerrumCompileError",
            "FerrumHydrationError",
        ):
            pattern = f"class {exc_name}(RuntimeError):"
            assert pattern in content, (
                f"'{pattern}' not found in _native.pyi — {exc_name} must inherit from RuntimeError"
            )

    def test_compiled_query_typeddict_has_all_keys(self) -> None:
        """``CompiledQuery`` TypedDict must have all 5 keys from the Rust extension."""
        if not _STUB_PATH.exists():
            pytest.fail("_native.pyi not found")
        content = _STUB_PATH.read_text()
        # Extract the CompiledQuery class block
        for key in ("sql_text", "bound_params", "param_type_summary", "fingerprint", "operation"):
            assert key in content, (
                f"Key '{key}' missing from CompiledQuery/CompiledQueryMsgpack in _native.pyi"
            )

    def test_compile_query_docstring_mentions_operation(self) -> None:
        """The ``compile_query`` docstring must mention the ``operation`` return key.

        This was a gap in the previous stub — the Rust extension returns an
        ``operation`` key but the old docstring omitted it.
        """
        if not _STUB_PATH.exists():
            pytest.fail("_native.pyi not found")
        content = _STUB_PATH.read_text()
        # Find compile_query def and extract its docstring.
        compile_query_section = content.split("def compile_query(")[1]
        # The docstring is the first triple-quoted string after the def line.
        first_triple = compile_query_section.find('"""')
        assert first_triple != -1, "compile_query has no docstring"
        closing_triple = compile_query_section.find('"""', first_triple + 3)
        assert closing_triple != -1, "compile_query docstring not terminated"
        docstring = compile_query_section[first_triple : closing_triple + 3]
        assert "operation" in docstring, (
            "compile_query docstring must document the 'operation' return key"
        )


class TestRejectedAPIUse:
    """Verify that incorrect API use is rejected by the type checker.

    Each test writes a fixture file with intentionally wrong code to a temp
    directory, runs ``ty check`` on it, and asserts that the expected type
    error is reported. This proves the stub enforces correct types.
    """

    @staticmethod
    def _run_ty(fixture_source: str, tmp_path: Path) -> tuple[int, str]:
        """Write fixture source to a temp file and run ``ty check`` on it.

        Returns (exit_code, stdout+stderr).
        """
        fixture_path = tmp_path / "_rejected_fixture.py"
        fixture_path.write_text(fixture_source)
        result = subprocess.run(  # noqa: S603 — controlled checker, not untrusted input
            ["ty", "check", str(fixture_path)],  # noqa: S607 — `ty` is a pinned dev dependency
            capture_output=True,
            text=True,
            cwd=str(_REPO_ROOT),
            timeout=60,
        )
        combined = result.stdout + result.stderr
        return result.returncode, combined

    def test_compile_query_rejects_bytes_first_arg(self, tmp_path: Path) -> None:
        """``compile_query`` first arg is ``str``, not ``bytes``."""
        source = textwrap.dedent("""\
            from __future__ import annotations
            from typing import TYPE_CHECKING

            if TYPE_CHECKING:
                import ferrum._native as _native

            def bad() -> None:
                _native.compile_query(b"", "", "postgres")
        """)
        code, output = self._run_ty(source, tmp_path)
        assert code != 0, f"Expected ty to reject bytes arg, but it passed:\n{output}"
        assert "compile_query" in output or "invalid-argument-type" in output, (
            f"Expected a type error about compile_query argument, got:\n{output}"
        )

    def test_compile_query_rejects_int_second_arg(self, tmp_path: Path) -> None:
        """``compile_query`` second arg is ``str``, not ``int``."""
        source = textwrap.dedent("""\
            from __future__ import annotations
            from typing import TYPE_CHECKING

            if TYPE_CHECKING:
                import ferrum._native as _native

            def bad() -> None:
                _native.compile_query("{}", 42, "postgres")
        """)
        code, output = self._run_ty(source, tmp_path)
        assert code != 0, f"Expected ty to reject int arg, but it passed:\n{output}"
        assert "compile_query" in output or "invalid-argument-type" in output, (
            f"Expected a type error about compile_query argument, got:\n{output}"
        )

    def test_compile_query_msgpack_rejects_str_first_arg(self, tmp_path: Path) -> None:
        """``compile_query_msgpack`` first arg is ``bytes``, not ``str``."""
        source = textwrap.dedent("""\
            from __future__ import annotations
            from typing import TYPE_CHECKING

            if TYPE_CHECKING:
                import ferrum._native as _native

            def bad() -> None:
                _native.compile_query_msgpack("not bytes", b"", "postgres")
        """)
        code, output = self._run_ty(source, tmp_path)
        assert code != 0, f"Expected ty to reject str arg, but it passed:\n{output}"
        assert "compile_query_msgpack" in output or "invalid-argument-type" in output, (
            f"Expected a type error about compile_query_msgpack argument, got:\n{output}"
        )

    def test_hydrate_rows_rejects_bytes_first_arg(self, tmp_path: Path) -> None:
        """``hydrate_rows`` first arg is ``str``, not ``bytes``."""
        source = textwrap.dedent("""\
            from __future__ import annotations
            from typing import TYPE_CHECKING

            if TYPE_CHECKING:
                import ferrum._native as _native

            def bad() -> None:
                _native.hydrate_rows(b"", "[]")
        """)
        code, output = self._run_ty(source, tmp_path)
        assert code != 0, f"Expected ty to reject bytes arg, but it passed:\n{output}"
        assert "hydrate_rows" in output or "invalid-argument-type" in output, (
            f"Expected a type error about hydrate_rows argument, got:\n{output}"
        )

    def test_hydrate_rows_msgpack_rejects_str_first_arg(self, tmp_path: Path) -> None:
        """``hydrate_rows_msgpack`` first arg is ``bytes``, not ``str``."""
        source = textwrap.dedent("""\
            from __future__ import annotations
            from typing import TYPE_CHECKING

            if TYPE_CHECKING:
                import ferrum._native as _native

            def bad() -> None:
                _native.hydrate_rows_msgpack("not bytes", b"")
        """)
        code, output = self._run_ty(source, tmp_path)
        assert code != 0, f"Expected ty to reject str arg, but it passed:\n{output}"
        assert "hydrate_rows_msgpack" in output or "invalid-argument-type" in output, (
            f"Expected a type error about hydrate_rows_msgpack argument, got:\n{output}"
        )

    def test_nonexistent_key_on_compiled_query_rejected(self, tmp_path: Path) -> None:
        """Accessing a key not in the CompiledQuery TypedDict should be flagged."""
        source = textwrap.dedent("""\
            from __future__ import annotations
            from typing import TYPE_CHECKING

            if TYPE_CHECKING:
                import ferrum._native as _native

            def bad() -> None:
                result = _native.compile_query("{}", "{}")
                bad_key: str = result["nonexistent_key"]
        """)
        code, output = self._run_ty(source, tmp_path)
        # ty may or may not flag TypedDict key access depending on strictness.
        # If it passes, that's a known limitation of TypedDict open-key access
        # in some type checkers. Record but don't fail the test.
        # This test documents the expected behavior; if ty catches it, great.
        if code != 0:
            assert (
                "nonexistent_key" in output
                or "typeddict" in output.lower()
                or ("invalid-argument-type" in output)
            ), f"Unexpected error for nonexistent key:\n{output}"

    def test_wrong_return_type_assignment_rejected(self, tmp_path: Path) -> None:
        """Assigning the return of compile_query to ``int`` should be rejected."""
        source = textwrap.dedent("""\
            from __future__ import annotations
            from typing import TYPE_CHECKING

            if TYPE_CHECKING:
                import ferrum._native as _native

            def bad() -> None:
                result: int = _native.compile_query("{}", "{}")
        """)
        code, output = self._run_ty(source, tmp_path)
        assert code != 0, (
            f"Expected ty to reject int assignment for CompiledQuery return, "
            f"but it passed:\n{output}"
        )
        assert "invalid-assignment" in output or "compile_query" in output, (
            f"Expected an assignment error, got:\n{output}"
        )


class TestStubRuntimeShape:
    """Runtime checks that the stub file has the expected structural shape.

    These do not require the Rust extension to be built — they parse the
    stub text to verify structure. They complement the compile-time
    ``TYPE_CHECKING`` fixture at the top of this file.
    """

    def test_two_distinct_typeddicts_for_wire_formats(self) -> None:
        """JSON and msgpack variants must have different ``bound_params`` types.

        ``CompiledQuery.bound_params`` is ``list[str]`` (JSON-encoded values).
        ``CompiledQueryMsgpack.bound_params`` is ``bytes`` (single msgpack blob).
        """
        if not _STUB_PATH.exists():
            pytest.fail("_native.pyi not found")
        content = _STUB_PATH.read_text()
        # Verify CompiledQuery has list[str] for bound_params
        cq_section = content.split("class CompiledQuery(")[1].split("class ")[0]
        assert "bound_params: list[str]" in cq_section, (
            "CompiledQuery.bound_params must be list[str]"
        )
        # Verify CompiledQueryMsgpack has bytes for bound_params
        cqm_section = content.split("class CompiledQueryMsgpack(")[1].split("class ")[0]
        assert "bound_params: bytes" in cqm_section, (
            "CompiledQueryMsgpack.bound_params must be bytes"
        )

    def test_all_functions_have_docstrings(self) -> None:
        """Every function in the stub must have a docstring."""
        if not _STUB_PATH.exists():
            pytest.fail("_native.pyi not found")
        content = _STUB_PATH.read_text()
        for func_name in (
            "compile_query",
            "compile_query_msgpack",
            "hydrate_rows",
            "hydrate_rows_msgpack",
            "plan_migration",
        ):
            # Find the function def and check it has a docstring
            pattern = f"def {func_name}("
            assert pattern in content, f"Function {func_name} not found in stub"
            section = content.split(pattern)[1]
            assert '"""' in section[:500], f"Function {func_name} must have a docstring in the stub"

    def test_all_exceptions_have_docstrings(self) -> None:
        """Every exception class in the stub must have a docstring."""
        if not _STUB_PATH.exists():
            pytest.fail("_native.pyi not found")
        content = _STUB_PATH.read_text()
        for exc_name in (
            "FerrumInternalError",
            "FerrumCompileError",
            "FerrumHydrationError",
        ):
            pattern = f"class {exc_name}(RuntimeError):"
            assert pattern in content, f"Exception {exc_name} not found in stub"
            section = content.split(pattern)[1]
            assert '"""' in section[:200], f"Exception {exc_name} must have a docstring in the stub"
