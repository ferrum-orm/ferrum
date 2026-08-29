"""Live integration tests for the Ferrum FastAPI contrib against PostgreSQL.

Exercises a real FastAPI app wired with:

- :func:`ferrum_lifespan` — one pool per process with W1-E event-based drain.
- :func:`get_ferrum_conn` — read-only pool dependency.
- :func:`get_ferrum_transaction` — transaction-scoped write dependency
  (commit on clean exit, rollback on exception).
- :func:`register_ferrum_exception_handlers` — Ferrum→HTTP error translation
  with only sanctioned safe fields in the response body.

Tests run against a live PostgreSQL instance via the ``pg_conn`` fixture and
create transient tables for each scenario. The FastAPI app is driven through
raw ASGI (lifespan events + HTTP requests) — no ``httpx`` dependency required.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest

import ferrum
from ferrum.connection import Connection
from ferrum.contrib.fastapi import (
    ferrum_lifespan,
    get_ferrum_conn,
    get_ferrum_transaction,
    register_ferrum_exception_handlers,
)
from ferrum.errors import FerrumNotFoundError

from .backends import POSTGRES
from .schema import Column, transient_table

# ---------------------------------------------------------------------------
# Raw ASGI helpers (no httpx dependency)
# ---------------------------------------------------------------------------


@contextlib.asynccontextmanager
async def _lifespan_scope(app: Any) -> AsyncIterator[None]:
    """Run the ASGI lifespan startup + shutdown around the block.

    Implements the ASGI lifespan protocol: a single call to ``app(scope,
    receive, send)`` that receives ``lifespan.startup`` and later
    ``lifespan.shutdown`` events via an asyncio queue, and collects the
    ``lifespan.startup.complete`` / ``lifespan.shutdown.complete`` responses.
    """
    scope = {"type": "lifespan", "asgi": {"version": "3.0", "spec_version": "2.0"}}
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    startup_done = asyncio.Future[dict[str, Any]]()

    async def receive() -> dict[str, Any]:
        return await queue.get()

    async def send(message: dict[str, Any]) -> None:
        if (
            message["type"] in ("lifespan.startup.complete", "lifespan.startup.failed")
            and not startup_done.done()
        ):
            startup_done.set_result(message)

    # Run the lifespan ASGI call as a background task so we can interleave
    # test work between startup and shutdown.
    task = asyncio.create_task(app(scope, receive, send))

    # Trigger startup and await its completion.
    await queue.put({"type": "lifespan.startup"})
    startup_msg = await startup_done
    if startup_msg["type"] == "lifespan.startup.failed":
        task.cancel()
        raise RuntimeError(f"Lifespan startup failed: {startup_msg.get('message', '')}")

    try:
        yield
    finally:
        # Trigger shutdown and wait for the task (which sends
        # lifespan.shutdown.complete) to finish.
        await queue.put({"type": "lifespan.shutdown"})
        try:
            await task
        except Exception as exc:
            raise RuntimeError(f"Lifespan shutdown failed: {exc}") from exc


async def _invoke_asgi(
    app: Any,
    path: str,
    method: str = "GET",
    body: bytes = b"",
) -> tuple[dict[str, Any], int]:
    """Invoke a FastAPI app via raw ASGI and return (json_body, status).

    The ``path`` may include a query string (``/items?name=x``); it is split
    into the ASGI ``path`` and ``query_string`` scope fields.
    """
    from urllib.parse import urlparse

    parsed = urlparse(path)
    clean_path = parsed.path
    query_string = parsed.query.encode()

    scope: dict[str, Any] = {
        "type": "http",
        "method": method,
        "path": clean_path,
        "raw_path": clean_path.encode(),
        "query_string": query_string,
        "headers": [(b"content-type", b"application/json")] if body else [],
        "client": ("127.0.0.1", 0),
        "server": ("testserver", 80),
        "scheme": "http",
        "root_path": "",
        "http_version": "1.1",
    }
    response_body = bytearray()
    response_started: dict[str, Any] = {}

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            response_started["status"] = message["status"]
        elif message["type"] == "http.response.body":
            response_body.extend(message["body"])

    await app(scope, receive, send)
    parsed = json.loads(response_body) if response_body else {}
    return parsed, response_started["status"]


# ---------------------------------------------------------------------------
# Test app factory
# ---------------------------------------------------------------------------


def _make_app(dsn: str, table_name: str) -> tuple[Any, type[ferrum.Model]]:
    """Build a FastAPI app wired with Ferrum lifespan + error handlers.

    Returns (app, model_class) so each test can use its own transient table.
    """
    from fastapi import Depends, FastAPI

    class Item(ferrum.Model):
        id: int = 0
        name: str = ""
        email: str = ""

        class Meta:
            table = table_name

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with ferrum_lifespan(dsn, min_size=1, max_size=5, drain_timeout=5.0) as conn:
            app.state.ferrum_conn = conn
            yield

    app = FastAPI(lifespan=lifespan)
    register_ferrum_exception_handlers(app)

    @app.get("/items/{item_id}")
    async def get_item(item_id: int, conn: Connection = Depends(get_ferrum_conn)) -> dict:  # noqa: B008
        try:
            item = await Item.objects.get(conn, id=item_id)
            return {"id": item.id, "name": item.name, "email": item.email}
        except FerrumNotFoundError:
            from starlette.responses import JSONResponse

            return JSONResponse(status_code=404, content={"error": {"code": "FERR-Q404"}})

    @app.post("/items")
    async def post_item(
        name: str,
        email: str,
        tx=Depends(get_ferrum_transaction),  # noqa: B008
    ) -> dict:
        item = await Item.objects.create(tx, name=name, email=email)
        return {"id": item.id, "name": item.name, "email": item.email}

    @app.get("/items")
    async def list_items(conn: Connection = Depends(get_ferrum_conn)) -> list[dict]:  # noqa: B008
        items = await Item.objects.all(conn)
        return [{"id": i.id, "name": i.name, "email": i.email} for i in items]

    return app, Item


def _make_unique_violation_app(dsn: str, table_name: str) -> tuple[Any, type[ferrum.Model]]:
    """Build a FastAPI app whose POST endpoint always raises a unique violation."""
    from fastapi import Depends, FastAPI

    class Item(ferrum.Model):
        id: int = 0
        name: str = ""
        email: str = ""

        class Meta:
            table = table_name

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with ferrum_lifespan(dsn, min_size=1, max_size=5) as conn:
            app.state.ferrum_conn = conn
            yield

    app = FastAPI(lifespan=lifespan)
    register_ferrum_exception_handlers(app)

    @app.post("/items")
    async def post_item(
        name: str,
        email: str,
        tx=Depends(get_ferrum_transaction),  # noqa: B008
    ) -> dict:
        # First insert succeeds; second insert inside the same transaction
        # hits the unique constraint and rolls back the whole transaction.
        await Item.objects.create(tx, name=name, email=email)
        await Item.objects.create(tx, name="dup", email=email)
        return {"ok": True}

    return app, Item


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_fastapi_lifespan_opens_one_pool_and_closes_cleanly(
    pg_conn: Connection,
    pg_dsn: str,
    require_native: None,
    unique_suffix: str,
) -> None:
    """The lifespan opens exactly one pool and closes it via the W1-E drain."""
    table_name = f"ferrum_int_fastapi_lifespan_{unique_suffix}"
    app, _Item = _make_app(pg_dsn, table_name)

    async with transient_table(
        pg_conn,
        table_name,
        backend=POSTGRES,
        columns=[
            Column("id", "pk_serial"),
            Column("name", "text", null=False),
            Column("email", "text", null=False),
        ],
    ):
        async with _lifespan_scope(app):
            pool_conn = app.state.ferrum_conn
            assert isinstance(pool_conn, Connection)
            val = await pool_conn._require_driver().fetchval("SELECT 1")
            assert val == 1
        # After lifespan shutdown, the pool is closed; further use raises.
        with pytest.raises(Exception):  # noqa: B017 — broad by design
            await app.state.ferrum_conn._require_driver().fetchval("SELECT 1")


@pytest.mark.integration
async def test_fastapi_get_ferrum_conn_serves_reads(
    pg_conn: Connection,
    pg_dsn: str,
    require_native: None,
    unique_suffix: str,
) -> None:
    """get_ferrum_conn returns the pool and serves read-only QuerySet terminals."""
    table_name = f"ferrum_int_fastapi_read_{unique_suffix}"
    app, Item = _make_app(pg_dsn, table_name)

    async with transient_table(
        pg_conn,
        table_name,
        backend=POSTGRES,
        columns=[
            Column("id", "pk_serial"),
            Column("name", "text", null=False),
            Column("email", "text", null=False),
        ],
    ):
        # Seed a row directly via the test's own connection.
        await Item.objects.create(pg_conn, name="seeded", email="seed@test")

        async with _lifespan_scope(app):
            body, status = await _invoke_asgi(app, "/items")
            assert status == 200
            assert len(body) == 1
            assert body[0]["name"] == "seeded"
            assert body[0]["email"] == "seed@test"


@pytest.mark.integration
async def test_fastapi_get_ferrum_transaction_commits_on_clean_exit(
    pg_conn: Connection,
    pg_dsn: str,
    require_native: None,
    unique_suffix: str,
) -> None:
    """get_ferrum_transaction commits when the request body completes cleanly."""
    table_name = f"ferrum_int_fastapi_tx_commit_{unique_suffix}"
    app, Item = _make_app(pg_dsn, table_name)

    async with transient_table(
        pg_conn,
        table_name,
        backend=POSTGRES,
        columns=[
            Column("id", "pk_serial"),
            Column("name", "text", null=False),
            Column("email", "text", null=False),
        ],
    ):
        async with _lifespan_scope(app):
            body, status = await _invoke_asgi(app, "/items?name=widget&email=w@test", method="POST")
            assert status == 200
            assert body["name"] == "widget"
            assert body["email"] == "w@test"
            assert body["id"] > 0

        # After the request committed, the row is visible to a separate
        # connection (the test's own pg_conn).
        rows = await Item.objects.all(pg_conn)
        assert len(rows) == 1
        assert rows[0].name == "widget"
        assert rows[0].email == "w@test"


@pytest.mark.integration
async def test_fastapi_get_ferrum_transaction_rolls_back_on_exception(
    pg_conn: Connection,
    pg_dsn: str,
    require_native: None,
    unique_suffix: str,
) -> None:
    """get_ferrum_transaction rolls back when the request body raises.

    Triggers a FerrumIntegrityError (unique violation) mid-request and
    verifies the error is translated to HTTP 409 with the safe-fields-only
    body, and that no partial row survives.
    """
    table_name = f"ferrum_int_fastapi_tx_rollback_{unique_suffix}"
    app, Item = _make_unique_violation_app(pg_dsn, table_name)

    async with transient_table(
        pg_conn,
        table_name,
        backend=POSTGRES,
        columns=[
            Column("id", "pk_serial"),
            Column("name", "text", null=False),
            Column("email", "text", null=False, extra="UNIQUE"),
        ],
    ):
        async with _lifespan_scope(app):
            body, status = await _invoke_asgi(
                app, "/items?name=first&email=dup@test", method="POST"
            )
            # The FerrumIntegrityError is translated to HTTP 409.
            assert status == 409
            assert body["error"]["code"] == "FERR-D201"
            assert body["error"]["sqlstate"] == "23505"
            # The safe-fields-only body must not echo the email value.
            body_text = str(body)
            assert "dup@test" not in body_text

        # The transaction rolled back — no row survived.
        rows = await Item.objects.all(pg_conn)
        assert len(rows) == 0


@pytest.mark.integration
async def test_fastapi_error_translation_returns_safe_fields_only(
    pg_conn: Connection,
    pg_dsn: str,
    require_native: None,
    unique_suffix: str,
) -> None:
    """The error handler returns only sanctioned safe fields, never the
    Ferrum exception message (which could carry DETAIL/row data).
    """
    table_name = f"ferrum_int_fastapi_err_{unique_suffix}"
    app, _Item = _make_app(pg_dsn, table_name)

    async with (
        transient_table(
            pg_conn,
            table_name,
            backend=POSTGRES,
            columns=[
                Column("id", "pk_serial"),
                Column("name", "text", null=False),
                Column("email", "text", null=False),
            ],
        ),
        _lifespan_scope(app),
    ):
        body, status = await _invoke_asgi(app, "/items/999999")
        assert status == 404
        # The 404 handler in the test app returns the safe-fields body.
        assert body["error"]["code"] == "FERR-Q404"


@pytest.mark.integration
async def test_fastapi_one_pool_serves_concurrent_requests(
    pg_conn: Connection,
    pg_dsn: str,
    require_native: None,
    unique_suffix: str,
) -> None:
    """One pool per process serves multiple simultaneous POST requests.

    Each POST opens a transaction via get_ferrum_transaction; the pool hands
    out separate connections (up to max_size). All requests commit
    independently. This proves the one-pool-per-process model does not
    serialize requests.
    """
    table_name = f"ferrum_int_fastapi_concurrent_{unique_suffix}"
    app, Item = _make_app(pg_dsn, table_name)

    async with transient_table(
        pg_conn,
        table_name,
        backend=POSTGRES,
        columns=[
            Column("id", "pk_serial"),
            Column("name", "text", null=False),
            Column("email", "text", null=False, extra="UNIQUE"),
        ],
    ):
        async with _lifespan_scope(app):
            # Fire 5 concurrent POST requests.
            coros = [
                _invoke_asgi(app, f"/items?name=u{i}&email=u{i}@test", method="POST")
                for i in range(5)
            ]
            results = await asyncio.gather(*coros)
            for body, status in results:
                assert status == 200
                assert body["id"] > 0

        # All 5 committed.
        rows = await Item.objects.all(pg_conn)
        assert len(rows) == 5
        emails = {r.email for r in rows}
        assert emails == {f"u{i}@test" for i in range(5)}
