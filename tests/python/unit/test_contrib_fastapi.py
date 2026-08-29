"""Unit tests for the Ferrum FastAPI contrib helpers.

Covers:

- :func:`ferrum_lifespan` — one pool per process, W1-E drain knobs threaded.
- :func:`get_ferrum_conn` — pool dependency.
- :func:`get_ferrum_transaction` — transaction-scoped dependency
  (commit on clean exit, rollback on exception).
- :func:`map_ferrum_to_http_status` — stable Ferrum→HTTP mapping.
- :func:`ferrum_exception_handler` — JSONResponse carrying only sanctioned
  safe fields; never leaks ``str(exc)``, DSNs, or bound values.
- :func:`register_ferrum_exception_handlers` — registers on a FastAPI app.
- Import boundary — core Ferrum modules never import ``fastapi`` /
  ``starlette`` / ``fastapi_users`` / ``ferrum.contrib``.
- :class:`FerrumUserDatabase` — soft-import behavior when
  ``fastapi_users`` is not installed.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from starlette.responses import JSONResponse

from ferrum.connection import Connection, Transaction
from ferrum.contrib.fastapi import (
    FerrumUserDatabase,
    ferrum_exception_handler,
    ferrum_lifespan,
    get_ferrum_conn,
    get_ferrum_transaction,
    map_ferrum_to_http_status,
    register_ferrum_exception_handlers,
)
from ferrum.errors import (
    FerrumCompileError,
    FerrumConfigError,
    FerrumConnectionError,
    FerrumDatabaseError,
    FerrumError,
    FerrumHydrationError,
    FerrumIntegrityError,
    FerrumInternalError,
    FerrumMigrationError,
    FerrumMultipleObjectsError,
    FerrumNotFoundError,
    FerrumRelationNotLoadedError,
    FerrumSchemaError,
    FerrumTimeoutError,
)

# ---------------------------------------------------------------------------
# ferrum_lifespan
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ferrum_lifespan_threads_pool_knobs_and_yields_connection() -> None:
    """Lifespan forwards W1-E pool knobs to ferrum.connect and yields the conn."""
    mock_conn = MagicMock(spec=Connection)
    captured: dict[str, Any] = {}

    @contextlib.asynccontextmanager
    async def fake_connect(
        database_url: str | None = None,
        *,
        min_size: int = 1,
        max_size: int = 10,
        acquire_timeout: float | None = None,
        query_timeout: float | None = None,
        statement_timeout: int | None = None,
        max_lifetime: float | None = None,
        max_idle_lifetime: float | None = None,
        max_connection_age: float | None = None,
        command_timeout: float | None = None,
        statement_cache_size: int | None = None,
        ssl: Any = None,
        server_settings: dict[str, str] | None = None,
        application_name: str | None = None,
        drain_timeout: float = 30.0,
        echo: bool | str = False,
    ):
        captured.update(
            database_url=database_url,
            min_size=min_size,
            max_size=max_size,
            acquire_timeout=acquire_timeout,
            query_timeout=query_timeout,
            statement_timeout=statement_timeout,
            drain_timeout=drain_timeout,
            echo=echo,
            application_name=application_name,
        )
        yield mock_conn

    with patch("ferrum.connection.connect", fake_connect):
        async with ferrum_lifespan(
            "postgresql://user@localhost/db",
            min_size=2,
            max_size=20,
            acquire_timeout=5.0,
            drain_timeout=10.0,
            application_name="ferrum-test",
        ) as conn:
            assert conn is mock_conn

    assert captured["database_url"] == "postgresql://user@localhost/db"
    assert captured["min_size"] == 2
    assert captured["max_size"] == 20
    assert captured["acquire_timeout"] == 5.0
    assert captured["drain_timeout"] == 10.0
    assert captured["application_name"] == "ferrum-test"


# ---------------------------------------------------------------------------
# get_ferrum_conn
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_ferrum_conn_returns_app_state_connection() -> None:
    app = FastAPI()
    conn = MagicMock(spec=Connection)
    app.state.ferrum_conn = conn

    request = MagicMock()
    request.app = app

    assert await get_ferrum_conn(request) is conn


@pytest.mark.asyncio
async def test_get_ferrum_conn_raises_when_uninitialized() -> None:
    app = FastAPI()
    request = MagicMock()
    request.app = app

    with pytest.raises(RuntimeError, match="not initialized"):
        await get_ferrum_conn(request)


# ---------------------------------------------------------------------------
# get_ferrum_transaction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_ferrum_transaction_yields_transaction_and_commits_on_clean_exit() -> None:
    """The transaction dependency commits when the request body completes cleanly."""
    app = FastAPI()
    conn = MagicMock(spec=Connection)
    tx = MagicMock(spec=Transaction)
    tx_cm = MagicMock()
    tx_cm.__aenter__ = AsyncMock(return_value=tx)
    tx_cm.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=tx_cm)
    app.state.ferrum_conn = conn

    request = MagicMock()
    request.app = app

    gen = get_ferrum_transaction(request)
    yielded = await gen.__anext__()
    assert yielded is tx
    # Clean exit — __aexit__ returns False (no exception suppression).
    with contextlib.suppress(StopAsyncIteration):
        await gen.aclose()
    conn.transaction.assert_called_once()


@pytest.mark.asyncio
async def test_get_ferrum_transaction_rolls_back_on_exception() -> None:
    """An exception in the request body propagates and the transaction rolls back."""
    app = FastAPI()
    conn = MagicMock(spec=Connection)
    tx = MagicMock(spec=Transaction)
    tx_cm = MagicMock()
    tx_cm.__aenter__ = AsyncMock(return_value=tx)
    tx_cm.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=tx_cm)
    app.state.ferrum_conn = conn

    request = MagicMock()
    request.app = app

    gen = get_ferrum_transaction(request)
    yielded = await gen.__anext__()
    assert yielded is tx
    # Simulate a request-body exception: the async with inside the dependency
    # re-raises; __aexit__ is called with the exc info and rolls back.
    with pytest.raises(ValueError):
        await gen.athrow(ValueError("boom"))
    conn.transaction.assert_called_once()


@pytest.mark.asyncio
async def test_get_ferrum_transaction_raises_when_uninitialized() -> None:
    app = FastAPI()
    request = MagicMock()
    request.app = app

    gen = get_ferrum_transaction(request)
    with pytest.raises(RuntimeError, match="not initialized"):
        await gen.__anext__()


# ---------------------------------------------------------------------------
# map_ferrum_to_http_status
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("exc", "expected_status"),
    [
        (FerrumNotFoundError("x"), 404),
        (FerrumIntegrityError("x", constraint="uq_users_email"), 409),
        (FerrumMultipleObjectsError("x"), 409),
        (FerrumCompileError("x"), 400),
        (FerrumRelationNotLoadedError("x"), 400),
        (FerrumConfigError("x"), 503),
        (FerrumTimeoutError("x"), 503),
        (FerrumConnectionError("x"), 503),
        (FerrumSchemaError("x"), 500),
        (FerrumHydrationError("x"), 500),
        (FerrumInternalError("x"), 500),
        (FerrumMigrationError("x"), 500),
        (FerrumDatabaseError("x"), 500),
        (FerrumError("x"), 500),
    ],
)
def test_map_ferrum_to_http_status(exc: FerrumError, expected_status: int) -> None:
    assert map_ferrum_to_http_status(exc) == expected_status


# ---------------------------------------------------------------------------
# ferrum_exception_handler
# ---------------------------------------------------------------------------


def test_ferrum_exception_handler_returns_json_response_with_safe_fields() -> None:
    exc = FerrumIntegrityError(
        "Unique constraint violation (uq_users_email). [FERR-D201]",
        constraint="uq_users_email",
        category="unique_violation",
        sqlstate="23505",
        model="User",
        operation="insert",
    )
    response = ferrum_exception_handler(MagicMock(), exc)
    assert isinstance(response, JSONResponse)
    assert response.status_code == 409
    # The payload carries only sanctioned safe fields.
    payload = _response_payload(response)
    assert payload["error"]["code"] == "FERR-D201"
    assert payload["error"]["category"] == "unique_violation"
    assert payload["error"]["sqlstate"] == "23505"
    assert payload["error"]["constraint"] == "uq_users_email"
    assert payload["error"]["model"] == "User"
    assert payload["error"]["operation"] == "insert"


def test_ferrum_exception_handler_does_not_leak_message_or_dsn() -> None:
    """The handler must never echo str(exc) — messages may carry sanitized text
    and the contract forbids echoing DETAIL/HINT/row data. The defensive boundary
    keeps the response shape minimal and stable.
    """
    sensitive_message = (
        "Duplicate key value violates unique constraint. "
        "DETAIL (key)=(user@example.com) already exists. "
        "DSN=postgresql://user:password@host:5432/db"
    )
    exc = FerrumIntegrityError(
        sensitive_message,
        constraint="uq_users_email",
        sqlstate="23505",
    )
    response = ferrum_exception_handler(MagicMock(), exc)
    payload = _response_payload(response)
    # The sensitive message must NOT appear anywhere in the response body.
    body_text = str(payload)
    assert "user@example.com" not in body_text
    assert "password" not in body_text
    assert "host" not in body_text
    assert "DETAIL" not in body_text
    assert "Duplicate key" not in body_text


def test_ferrum_exception_handler_omits_none_fields() -> None:
    exc = FerrumNotFoundError("not found")
    response = ferrum_exception_handler(MagicMock(), exc)
    payload = _response_payload(response)
    assert payload["error"]["code"] == "FERR-Q404"
    # Optional fields that are None are omitted, not null.
    assert "sqlstate" not in payload["error"]
    assert "constraint" not in payload["error"]
    assert "model" not in payload["error"]


def test_register_ferrum_exception_handlers_registers_on_app() -> None:
    """Registering the handler attaches it to the FastAPI app's exception
    handler table under the FerrumError base class.
    """
    app = FastAPI()
    register_ferrum_exception_handlers(app)
    # FastAPI stores exception handlers in the router's exception_handlers
    # table keyed by exception class. The FerrumError base must be present.
    handlers = getattr(app, "exception_handlers", None) or getattr(
        app.router, "exception_handlers", {}
    )
    assert FerrumError in handlers
    assert handlers[FerrumError] is ferrum_exception_handler


def test_register_ferrum_exception_handlers_roundtrip_via_asgi() -> None:
    """End-to-end ASGI roundtrip: a FastAPI app wired with the handler
    returns the mapped status and the safe-fields-only body for each
    Ferrum error class. Uses a minimal raw-ASGI call so the test does not
    depend on the optional ``httpx`` transport that TestClient requires.
    """
    app = FastAPI()
    register_ferrum_exception_handlers(app)

    @app.get("/integrity")
    async def integrity() -> None:
        raise FerrumIntegrityError("dup", sqlstate="23505", constraint="uq")

    @app.get("/timeout")
    async def timeout() -> None:
        raise FerrumTimeoutError("slow")

    @app.get("/config")
    async def config() -> None:
        raise FerrumConfigError("no native")

    body_integrity, status_integrity = asyncio.run(_invoke_asgi(app, "/integrity"))
    assert status_integrity == 409
    assert body_integrity["error"]["code"] == "FERR-D201"
    assert body_integrity["error"]["sqlstate"] == "23505"

    _, status_timeout = asyncio.run(_invoke_asgi(app, "/timeout"))
    assert status_timeout == 503

    _, status_config = asyncio.run(_invoke_asgi(app, "/config"))
    assert status_config == 503


# ---------------------------------------------------------------------------
# Import boundary — core Ferrum never imports FastAPI / fastapi-users
# ---------------------------------------------------------------------------


def test_core_ferrum_does_not_import_fastapi_or_starlette() -> None:
    """Importing every public core Ferrum query-path module must not pull in
    ``fastapi``, ``starlette``, or ``fastapi_users``.

    The ``.importlinter`` contracts (``cli-isolation``, ``contrib-isolation``)
    enforce this in CI; this unit test is an explicit, in-isolation proof that
    runs in a clean subprocess so the test file's own FastAPI imports do not
    pollute ``sys.modules``.

    Note: the top-level ``ferrum`` package (``ferrum/__init__.py``) does import
    ``ferrum.contrib`` for convenience, which is expected — the security
    boundary is that the *query-path* modules (connection, queryset, models,
    errors, hooks, migrations, session, runtime, relations, registry, config)
    never import FastAPI or Starlette. This matches the import-linter contract
    source_modules exactly.
    """
    import subprocess
    import sys

    checker = (
        "import sys\n"
        # Import the top-level package first (it imports ferrum.contrib,
        # which is expected). Then check that the query-path modules don't
        # pull in fastapi/starlette/fastapi_users.
        "core_modules = [\n"
        "    'ferrum.connection', 'ferrum.queryset', 'ferrum.models',\n"
        "    'ferrum.errors', 'ferrum.hooks', 'ferrum.migrations', 'ferrum.session',\n"
        "    'ferrum.runtime', 'ferrum.relations', 'ferrum.registry', 'ferrum.config',\n"
        "]\n"
        "forbidden_prefixes = ('fastapi', 'starlette', 'fastapi_users')\n"
        "for mod in core_modules:\n"
        "    __import__(mod)\n"
        "leaked = [\n"
        "    name for name in sys.modules\n"
        "    for prefix in forbidden_prefixes\n"
        "    if name == prefix or name.startswith(prefix + '.')\n"
        "]\n"
        "print(sorted(leaked))\n"
    )
    result = subprocess.run(  # noqa: S603 — controlled checker string, not untrusted input
        [sys.executable, "-c", checker],
        capture_output=True,
        text=True,
        check=True,
        cwd=".",
    )
    leaked = eval(result.stdout.strip())  # noqa: S307 — controlled output from our own subprocess
    assert not leaked, f"Core Ferrum pulled in forbidden modules: {leaked}"


def test_contrib_package_init_does_not_import_fastapi() -> None:
    """``import ferrum.contrib`` must not import FastAPI — the contrib package
    keeps FastAPI as a soft import inside ``ferrum.contrib.fastapi`` only.

    Runs in a clean subprocess for the same reason as the core boundary test.
    """
    import subprocess
    import sys

    checker = (
        "import sys\n"
        "import ferrum.contrib\n"
        "leaked = [n for n in sys.modules if n in ('fastapi', 'starlette')]\n"
        "print(sorted(leaked))\n"
    )
    result = subprocess.run(  # noqa: S603 — controlled checker string, not untrusted input
        [sys.executable, "-c", checker],
        capture_output=True,
        text=True,
        check=True,
        cwd=".",
    )
    leaked = eval(result.stdout.strip())  # noqa: S307 — controlled output from our own subprocess
    assert not leaked, f"ferrum.contrib __init__ pulled in FastAPI/Starlette: {leaked}"


# ---------------------------------------------------------------------------
# FerrumUserDatabase — soft import behavior
# ---------------------------------------------------------------------------


def test_ferrum_user_database_construction_raises_without_fastapi_users() -> None:
    """When ``fastapi_users`` is not installed, constructing the adapter raises
    a clear ImportError (not a silent failure or a ModuleNotFound at first use).
    """
    # fastapi_users is not a Ferrum dev dependency; if it happens to be
    # installed in the test environment, skip this test rather than fiddle
    # with sys.modules to fake its absence.
    pytest.importorskip(
        "fastapi_users",
        reason="fastapi_users IS installed — skip the not-installed branch",
    )
    # If we reach here, fastapi_users is NOT installed; construction must raise.
    with pytest.raises(ImportError, match="fastapi-users"):
        FerrumUserDatabase(user_model=object())


def test_ferrum_user_database_construction_succeeds_with_fastapi_users() -> None:
    """When ``fastapi_users`` IS installed, construction succeeds."""
    pytest.importorskip("fastapi_users")
    FerrumUserDatabase(user_model=object())


def test_ferrum_user_database_get_by_id_returns_none_on_not_found() -> None:
    """get_by_id translates FerrumNotFoundError into ``None`` (BaseUserDatabase contract)."""
    pytest.importorskip("fastapi_users")

    class _FakeObjects:
        @staticmethod
        async def get(conn: Any, **kwargs: Any) -> Any:
            raise FerrumNotFoundError("not found")

    class FakeUser:
        objects = _FakeObjects()

    db = FerrumUserDatabase(user_model=FakeUser)
    result = asyncio.run(db.get_by_id(MagicMock(spec=Connection), id=1))
    assert result is None


def test_ferrum_user_database_create_translates_integrity_to_user_already_exists() -> None:
    """create() translates FerrumIntegrityError into fastapi_users.UserAlreadyExists."""
    pytest.importorskip("fastapi_users")
    from fastapi_users.exceptions import UserAlreadyExists

    class _FakeObjects:
        @staticmethod
        async def create(conn: Any, create_dict: dict[str, Any]) -> Any:
            raise FerrumIntegrityError("dup", sqlstate="23505")

    class FakeUser:
        objects = _FakeObjects()

    db = FerrumUserDatabase(user_model=FakeUser)
    with pytest.raises(UserAlreadyExists):
        asyncio.run(db.create(MagicMock(spec=Connection), {"email": "a@b.c"}))


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _response_payload(response: JSONResponse) -> dict[str, Any]:
    """Render a JSONResponse body without going through the ASGI transport."""
    return json.loads(response.body)


async def _invoke_asgi(app: FastAPI, path: str) -> tuple[dict[str, Any], int]:
    """Invoke a FastAPI app via the raw ASGI protocol and return (json_body, status).

    Avoids ``starlette.testclient.TestClient`` (which requires ``httpx``).
    """
    scope = {
        "type": "http",
        "method": "GET",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 0),
        "server": ("testserver", 80),
        "scheme": "http",
        "root_path": "",
        "http_version": "1.1",
    }
    response_body = bytearray()
    response_started: dict[str, Any] = {}

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            response_started["status"] = message["status"]
        elif message["type"] == "http.response.body":
            response_body.extend(message["body"])

    await app(scope, receive, send)
    body = json.loads(response_body) if response_body else {}
    return body, response_started["status"]
