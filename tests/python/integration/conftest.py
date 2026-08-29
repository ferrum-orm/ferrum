"""Integration-test fixtures for multi-driver and PostgreSQL-only tests.

## Multi-backend tests

Use ``db_conn`` (parameterized over all active backends) together with
``backend`` (the Backend descriptor) and ``requires`` (capability guard).
Both ``db_conn`` and ``backend`` share a single root fixture ``_backend_param``
that is parameterized by ``pytest_generate_tests``, so the same ``Backend``
object is always consistent across the two fixtures within one test run.

Active backends are those whose DSN env var is set:

    FERRUM_TEST_DSN          → postgres   (also activates pg_conn / pg_dsn)
    FERRUM_TEST_MYSQL_DSN    → mysql
    FERRUM_TEST_SQLITE_DSN   → sqlite
    FERRUM_TEST_MSSQL_DSN    → mssql

## Fail-loud guard

Set ``FERRUM_TEST_REQUIRE_BACKENDS=postgres,sqlite`` to make the session fail
with a clear error when those backends are missing.  Without this, a misnamed
env var produces a green job that ran zero tests.

## PostgreSQL-only tests

Tests that are genuinely Postgres-specific (RLS, pgvector, Postgres pool
internals) keep ``pg_conn`` / ``pg_dsn`` and are unaffected by the
multi-backend parameterization.

## Capability gating (two styles)

**Marker** (preferred — skips before any setup runs):

    @pytest.mark.requires_capability(Capability.STREAMING)
    async def test_streaming(db_conn):
        ...

**In-test callable** (skip after per-test setup, if needed):

    async def test_streaming(db_conn, backend, requires):
        requires(Capability.STREAMING)
        ...

Both styles require ``backend`` or ``db_conn`` to appear in the fixture list
so that ``pytest_generate_tests`` activates multi-backend parameterization.
The marker accepts exactly one ``Capability`` value or its string name;
anything else raises ``pytest.UsageError`` at collection time.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable

import pytest

import ferrum

from .backends import ALL_BACKENDS, Backend, Capability

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _active_backends() -> list[Backend]:
    """Return backends whose DSN env var is currently set."""
    return [b for b in ALL_BACKENDS if os.environ.get(b.dsn_env)]


def _required_backend_names() -> list[str]:
    """Names from FERRUM_TEST_REQUIRE_BACKENDS (comma-separated, lowercased)."""
    raw = os.environ.get("FERRUM_TEST_REQUIRE_BACKENDS", "")
    return [n.strip().lower() for n in raw.split(",") if n.strip()]


# ---------------------------------------------------------------------------
# Session-level hooks
# ---------------------------------------------------------------------------


def pytest_configure(config: pytest.Config) -> None:
    """Register markers and validate FERRUM_TEST_REQUIRE_BACKENDS early."""
    config.addinivalue_line(
        "markers",
        "requires_capability(cap): skip test when the active backend lacks cap",
    )
    required = _required_backend_names()
    if not required:
        return
    by_name = {b.name: b for b in ALL_BACKENDS}
    for name in required:
        b = by_name.get(name)
        if b is None:
            raise pytest.UsageError(
                f"FERRUM_TEST_REQUIRE_BACKENDS lists unknown backend {name!r}. "
                f"Known backends: {sorted(by_name)}"
            )
        if not os.environ.get(b.dsn_env):
            raise pytest.UsageError(
                f"Backend {name!r} is required (FERRUM_TEST_REQUIRE_BACKENDS) "
                f"but {b.dsn_env} is not set. "
                f"Set {b.dsn_env} or remove {name!r} from "
                f"FERRUM_TEST_REQUIRE_BACKENDS."
            )


def pytest_runtest_setup(item: pytest.Item) -> None:
    """Enforce ``@pytest.mark.requires_capability`` against the active backend.

    Skips the test when the parameterized backend lacks the declared capability.
    Raises :class:`pytest.UsageError` for an invalid marker argument (wrong
    type, unrecognized name, or not exactly one argument).

    Only meaningful for tests parameterized over ``_backend_param``.  Tests
    that use ``pg_conn`` directly (Postgres-only) are left unaffected.
    """
    markers = list(item.iter_markers("requires_capability"))
    if not markers:
        return

    # Resolve the active backend from the indirect callspec param, if present.
    backend: Backend | None = None
    if hasattr(item, "callspec") and "_backend_param" in item.callspec.params:
        backend = item.callspec.params["_backend_param"]
    if backend is None:
        # Test is not parameterized over a backend; nothing to gate.
        return

    for marker in markers:
        if len(marker.args) != 1:
            raise pytest.UsageError(
                f"@pytest.mark.requires_capability on {item.nodeid!r} requires "
                "exactly one argument: a Capability value or its string name."
            )
        cap_arg = marker.args[0]
        if isinstance(cap_arg, Capability):
            cap = cap_arg
        elif isinstance(cap_arg, str):
            try:
                cap = Capability(cap_arg)
            except ValueError:
                valid = ", ".join(repr(c.value) for c in Capability)
                raise pytest.UsageError(
                    f"@pytest.mark.requires_capability: {cap_arg!r} is not a "
                    f"valid Capability name. Valid values: {valid}"
                ) from None
        else:
            raise pytest.UsageError(
                f"@pytest.mark.requires_capability: argument must be a Capability "
                f"or string, got {type(cap_arg).__name__!r}"
            )
        if cap not in backend.capabilities:
            pytest.skip(f"Backend {backend.name!r} does not support {cap.value!r}")


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    """Parameterize ``_backend_param`` over every active backend.

    The check covers both ``db_conn`` and ``backend`` as entry points so that
    tests using either fixture (or ``requires``, which depends on ``backend``)
    are parameterized correctly.

    When no backend DSN is set the fixture receives ``None`` and the test is
    skipped inside the fixture body — this makes the skip visible rather than
    showing zero collected tests.
    """
    needs_backend = "db_conn" in metafunc.fixturenames or "backend" in metafunc.fixturenames
    if not needs_backend:
        return

    active = _active_backends()
    if not active:
        params: list[Backend | None] = [None]
        ids = ["no-backend"]
    else:
        params = list(active)  # type: ignore[assignment]
        ids = [b.name for b in active]

    metafunc.parametrize("_backend_param", params, indirect=True, ids=ids)


# ---------------------------------------------------------------------------
# Parameterization root fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def _backend_param(request: pytest.FixtureRequest) -> Backend | None:
    """Internal root fixture; holds the active Backend for this test run.

    Parameterized by ``pytest_generate_tests`` via ``indirect=True``.  Never
    reference this fixture directly in tests — use ``db_conn`` or ``backend``.
    """
    # getattr guards against non-indirect calls (e.g., if a test accidentally
    # requests this fixture without going through pytest_generate_tests).
    return getattr(request, "param", None)  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# Existing fixtures — kept unchanged for PostgreSQL-specific tests
# ---------------------------------------------------------------------------


@pytest.fixture
def require_native() -> None:
    """Skip when the maturin-built Rust extension is not importable."""
    pytest.importorskip(
        "ferrum._native",
        reason="Rust extension not built — run `maturin develop`",
    )


@pytest.fixture
def unique_suffix() -> str:
    """Unique hex suffix for transient table names (parallel-safe)."""
    return uuid.uuid4().hex[:12]


@pytest.fixture
def pg_dsn() -> str:
    """Raw DSN string for tests that manage their own connection lifecycle.

    Skips when ``FERRUM_TEST_DSN`` is not set.
    """
    dsn = os.environ.get("FERRUM_TEST_DSN")
    if not dsn:
        pytest.skip("FERRUM_TEST_DSN not set")
    return dsn


@pytest.fixture
async def pg_conn() -> ferrum.connection.Connection:
    """Open Ferrum connection backed by a live PostgreSQL instance.

    Skips when ``FERRUM_TEST_DSN`` is not set.  Kept for tests that are
    genuinely Postgres-specific (RLS, pgvector, pool internals).
    """
    dsn = os.environ.get("FERRUM_TEST_DSN")
    if not dsn:
        pytest.skip("FERRUM_TEST_DSN not set")
    async with ferrum.connect(dsn) as conn:
        yield conn


# ---------------------------------------------------------------------------
# New multi-backend fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def db_conn(_backend_param: Backend | None) -> ferrum.connection.Connection:
    """Open Ferrum connection for the current backend parameter.

    Parameterized over all backends whose DSN env var is set.  Tests that are
    genuinely Postgres-only should keep using ``pg_conn`` instead.

    Use alongside ``backend`` for type mappings and capability flags, and
    ``requires`` for per-capability skipping::

        async def test_create(db_conn, backend, unique_suffix):
            async with transient_table(
                db_conn, f"t_{unique_suffix}",
                backend=backend,
                columns=[Column("id", "pk_serial")],
            ) as conn:
                ...
    """
    b = _backend_param
    if b is None:
        pytest.skip("No backend DSN configured — set at least one FERRUM_TEST_*_DSN")
    dsn = os.environ.get(b.dsn_env)
    if not dsn:
        pytest.skip(f"{b.dsn_env} is not set")
    async with ferrum.connect(dsn) as conn:
        yield conn


@pytest.fixture
def backend(_backend_param: Backend | None) -> Backend:
    """Backend descriptor for the current ``db_conn`` parameter.

    Provides ``name``, ``capabilities``, ``types``, and ``quote`` for the
    active backend.  Always use together with ``db_conn`` so the backend is
    known::

        async def test_something(db_conn, backend, unique_suffix):
            async with transient_table(
                db_conn, f"t_{unique_suffix}",
                backend=backend,
                columns=[Column("id", "pk_serial"), Column("name", "text")],
            ) as conn:
                ...
    """
    b = _backend_param
    if b is None:
        pytest.skip("No backend configured")
    return b


@pytest.fixture
def requires(backend: Backend) -> Callable[[Capability], None]:
    """Return a guard callable that skips when the backend lacks a capability.

    The guard is invoked inside the test body::

        async def test_streaming(db_conn, backend, requires):
            requires(Capability.STREAMING)
            # remainder only runs on backends that support streaming

    Tests using ``requires`` must also declare ``backend`` (or ``db_conn``)
    so that ``pytest_generate_tests`` activates multi-backend parameterization.
    """

    def _check(cap: Capability) -> None:
        if cap not in backend.capabilities:
            pytest.skip(f"Backend {backend.name!r} does not support {cap.value!r}")

    return _check
