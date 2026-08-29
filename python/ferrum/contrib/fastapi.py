"""FastAPI / Starlette integration helpers for Ferrum.

Provides:

- :func:`ferrum_lifespan` — one-pool-per-process ASGI lifespan backed by the
  W1-E event-based pool drain.
- :func:`get_ferrum_conn` — request dependency returning the pool
  :class:`~ferrum.connection.Connection` (read-only / non-transactional use).
- :func:`get_ferrum_transaction` — request dependency yielding a
  :class:`~ferrum.connection.Transaction` that commits on clean exit and
  rolls back on exception (transaction-scoped writes).
- :func:`register_ferrum_exception_handlers` — registers Ferrum→HTTP error
  translation on a FastAPI app. Only sanctioned safe fields
  (``code``/``category``/``sqlstate``/``constraint``/``model``/``operation``)
  ever appear in a response body. DSNs, bound parameter values, and row data
  are never exposed (CRED-1, ERR-1).
- :class:`FerrumUserDatabase` — optional ``fastapi-users`` adapter
  (``BaseUserDatabase`` protocol) for user lookup/create/update/delete and
  OAuth account relations. Soft-imports ``fastapi_users``; importing it
  without the extra installed raises a clear ``ImportError``.

This module is part of the optional ``ferrum[fastapi]`` extra. Core Ferrum
never imports it (enforced by import-linter ``cli-isolation`` and
``contrib-isolation`` contracts, plus an explicit unit test in
``tests/python/unit/test_contrib_fastapi.py``).
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Any, Protocol

from ferrum.connection import Connection, Transaction
from ferrum.errors import (
    FerrumCompileError,
    FerrumConfigError,
    FerrumConnectionError,
    FerrumError,
    FerrumIntegrityError,
    FerrumMultipleObjectsError,
    FerrumNotFoundError,
    FerrumRelationNotLoadedError,
    FerrumTimeoutError,
)

if TYPE_CHECKING:
    from ssl import SSLContext

# Starlette Request is imported eagerly (not under TYPE_CHECKING) so FastAPI's
# dependency-injection introspection sees the concrete type. Fall back to Any
# when Starlette is not installed — the module is still importable, but using
# get_ferrum_conn / get_ferrum_transaction as Depends requires Starlette.
try:
    from starlette.requests import Request as _StarletteRequest
except ImportError:  # pragma: no cover — exercised by import-boundary tests
    _StarletteRequest = Any  # type: ignore[assignment]  # ty: ignore[invalid-assignment]

# ---------------------------------------------------------------------------
# ASGI / FastAPI structural types
# ---------------------------------------------------------------------------


class _ASGIAppWithState(Protocol):
    state: object


class FerrumConnRequest(Protocol):
    """Structural type for ASGI requests passed to :func:`get_ferrum_conn`.

    Satisfied by Starlette/FastAPI ``Request`` and any object exposing
    ``app.state`` (where lifespan setup stores ``ferrum_conn``).

    Note: when used as a FastAPI ``Depends`` parameter, the function
    signature uses ``starlette.requests.Request`` (aliased as
    ``_StarletteRequest`` at import time) so FastAPI's dependency injection
    recognizes it as the ASGI request object. This Protocol exists for
    static type-checking of the helper signatures in environments where
    Starlette is not installed.
    """

    app: _ASGIAppWithState


# ---------------------------------------------------------------------------
# Lifespan: one pool per process with W1-E event-based drain
# ---------------------------------------------------------------------------


@contextlib.asynccontextmanager
async def ferrum_lifespan(
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
    ssl: bool | str | SSLContext | None = None,
    server_settings: dict[str, str] | None = None,
    application_name: str | None = None,
    drain_timeout: float = 30.0,
    echo: bool | str = False,
) -> AsyncGenerator[Connection, None]:
    """ASGI lifespan context manager that owns the Ferrum pool for a process.

    Opens exactly one :class:`~ferrum.connection.Connection` (pool) per
    process and yields it. Assign it to ``app.state.ferrum_conn`` so route
    handlers can inject it via :func:`get_ferrum_conn` or
    :func:`get_ferrum_transaction`.

    The pool is closed via ``Connection.close()`` which uses the W1-E
    :class:`~ferrum.connection._EventLifecycleGuard` event-based drain:
    in-flight work completes (awaited via ``asyncio.Event``, not busy
    polling), then the pool closes. If ``drain_timeout`` elapses the pool is
    still closed (no connection leak) and a :class:`FerrumTimeoutError` is
    raised to report the forced drain timeout — surfaced to the ASGI server
    as a lifespan shutdown error.

    The DSN is never logged (CRED-1). When ``database_url`` is omitted, the
    same environment resolution as :func:`ferrum.connect` applies
    (``FERRUM_DATABASE_URL``, then ``DATABASE_URL``).

    Usage::

        from contextlib import asynccontextmanager
        from fastapi import FastAPI
        from ferrum.contrib.fastapi import ferrum_lifespan

        @asynccontextmanager
        async def lifespan(app: FastAPI):
            async with ferrum_lifespan(
                min_size=2, max_size=20, acquire_timeout=5.0, drain_timeout=10.0,
            ) as conn:
                app.state.ferrum_conn = conn
                yield

        app = FastAPI(lifespan=lifespan)

    Args:
        database_url: DSN. When ``None``, resolved from environment.
        min_size / max_size: pool sizing bounds.
        acquire_timeout: seconds to wait for a pooled connection on every
            acquire path. ``None`` means the driver default (no explicit
            timeout).
        query_timeout: per-query Python-side deadline (seconds).
        statement_timeout: server-side ``statement_timeout`` (milliseconds).
        max_lifetime: legacy alias for ``max_idle_lifetime``.
        max_idle_lifetime: recycle idle connections after this many seconds.
        max_connection_age: hard max age for connections (seconds).
        command_timeout: per-command timeout (seconds, PostgreSQL only).
        statement_cache_size: asyncpg prepared-statement cache size.
        ssl: SSL configuration (``True``, mode string, ``SSLContext``, or
            ``None``). PostgreSQL only.
        server_settings: PostgreSQL GUC overrides (``dict[str, str]``).
        application_name: PostgreSQL ``application_name``.
        drain_timeout: seconds to wait for in-flight work on shutdown.
            Backs the W1-E event-based drain.
        echo: SQLAlchemy-like console logging (``True``/``"sql"``/``"debug"``).

    Yields:
        The open pool :class:`~ferrum.connection.Connection`.
    """
    from ferrum.connection import connect

    async with connect(
        database_url,
        min_size=min_size,
        max_size=max_size,
        acquire_timeout=acquire_timeout,
        query_timeout=query_timeout,
        statement_timeout=statement_timeout,
        max_lifetime=max_lifetime,
        max_idle_lifetime=max_idle_lifetime,
        max_connection_age=max_connection_age,
        command_timeout=command_timeout,
        statement_cache_size=statement_cache_size,
        ssl=ssl,
        server_settings=server_settings,
        application_name=application_name,
        drain_timeout=drain_timeout,
        echo=echo,
    ) as conn:
        yield conn


# ---------------------------------------------------------------------------
# Request dependencies
# ---------------------------------------------------------------------------


async def get_ferrum_conn(request: _StarletteRequest) -> Connection:
    """FastAPI dependency returning the pool opened during app lifespan.

    Use for read-only or non-transactional work. Each terminal call acquires
    its own pooled connection (autocommit). For a request-scoped transaction
    that commits/rolls back with the request, use
    :func:`get_ferrum_transaction` instead.

    Raises:
        RuntimeError: if the lifespan did not set ``app.state.ferrum_conn``
            to an open :class:`~ferrum.connection.Connection`.
    """
    conn = getattr(request.app.state, "ferrum_conn", None)
    if not isinstance(conn, Connection):
        raise RuntimeError(
            "Ferrum connection is not initialized. In the app lifespan, open "
            "ferrum_lifespan and set app.state.ferrum_conn = conn."
        )
    return conn


async def get_ferrum_transaction(request: _StarletteRequest) -> AsyncGenerator[Transaction, None]:
    """FastAPI dependency yielding a request-scoped transaction.

    Opens a transaction on the pool :class:`~ferrum.connection.Connection`
    set by :func:`ferrum_lifespan`, yields a
    :class:`~ferrum.connection.Transaction` for the request body, commits on
    clean exit, and rolls back on any exception. The transaction pins one
    pooled connection for the entire request, matching the ratified §5a
    object-scoped retry-disable rule: statements issued through the
    ``Transaction`` never statement-retry.

    Usage::

        from fastapi import Depends
        from ferrum.connection import Transaction
        from ferrum.contrib.fastapi import get_ferrum_transaction

        @app.post("/items")
        async def create_item(tx: Transaction = Depends(get_ferrum_transaction)):
            item = await Item.objects.create(tx, name="widget")
            return item

    Yields:
        A :class:`~ferrum.connection.Transaction` usable anywhere a
        ``Connection`` is accepted.
    """
    conn = getattr(request.app.state, "ferrum_conn", None)
    if not isinstance(conn, Connection):
        raise RuntimeError(
            "Ferrum connection is not initialized. In the app lifespan, open "
            "ferrum_lifespan and set app.state.ferrum_conn = conn."
        )
    async with conn.transaction() as tx:
        yield tx


# ---------------------------------------------------------------------------
# Ferrum → HTTP error translation
# ---------------------------------------------------------------------------


def map_ferrum_to_http_status(exc: FerrumError) -> int:
    """Map a :class:`FerrumError` to an HTTP status code.

    Mapping (ratified §5a — only sanctioned safe fields ever leave Ferrum):

    - :class:`FerrumNotFoundError` → 404 (the resource the client asked for
      does not exist).
    - :class:`FerrumIntegrityError` → 409 (constraint violation — unique, FK,
      not-null, check; the request conflicts with persisted state).
    - :class:`FerrumMultipleObjectsError` → 409 (more than one row matched a
      ``get()`` — a server-side invariant; 409 surfaces it as a conflict).
    - :class:`FerrumCompileError` / :class:`FerrumRelationNotLoadedError` →
      400 (the request asked for a field/operator/relation the model does
      not expose; client-side problem).
    - :class:`FerrumConfigError` → 503 (server misconfiguration — the pool
      is not open, the native extension is not built; service unavailable).
    - :class:`FerrumTimeoutError` → 503 (timeout, lock_timeout,
      query_cancellation — service unavailable).
    - :class:`FerrumConnectionError` → 503 (connection / pool / failover —
      service unavailable).
    - :class:`FerrumSchemaError` → 500 (undefined table/column/function is a
      server-side schema drift; not a client error).
    - :class:`FerrumHydrationError` / :class:`FerrumInternalError` → 500
      (Rust panic or hydration failure; server-side).
    - :class:`FerrumMigrationError` → 500 (migration apply/revert failure;
      server-side).
    - :class:`FerrumDatabaseError` (and the base :class:`FerrumError`) → 500.

    The mapping is stable and exists in one place so handlers and tests
    agree. It never inspects ``str(exc)`` (which could carry a sanitized
    message) — only the exception class and the ratified safe attributes
    (``category``, ``sqlstate``).
    """
    if isinstance(exc, FerrumNotFoundError):
        return 404
    if isinstance(exc, FerrumIntegrityError | FerrumMultipleObjectsError):
        return 409
    if isinstance(exc, FerrumCompileError | FerrumRelationNotLoadedError):
        return 400
    if isinstance(exc, FerrumConfigError | FerrumTimeoutError | FerrumConnectionError):
        return 503
    # FerrumSchemaError, FerrumHydrationError, FerrumInternalError,
    # FerrumMigrationError, FerrumDatabaseError, and the base FerrumError.
    return 500


def _sanitize_ferrum_error_payload(exc: FerrumError) -> dict[str, Any]:
    """Build a JSON-ready payload carrying only sanctioned safe fields.

    Per ratified §5a the safe-error-field set is exactly ``sqlstate``,
    ``category``, ``constraint``, ``model``/``operation``. The Ferrum
    ``code`` (e.g. ``FERR-D201``) is also safe — it is a fixed enum-style
    string that names the error class, never user data. ``str(exc)`` is
    intentionally NOT included: messages are sanitized but the contract
    forbids echoing DETAIL/HINT/row data, and a defensive boundary keeps
    the response shape minimal.
    """
    payload: dict[str, Any] = {"code": exc.code}
    if exc.category is not None:
        payload["category"] = exc.category
    if exc.sqlstate is not None:
        payload["sqlstate"] = exc.sqlstate
    if exc.constraint is not None:
        payload["constraint"] = exc.constraint
    if exc.model is not None:
        payload["model"] = exc.model
    if exc.operation is not None:
        payload["operation"] = exc.operation
    return payload


def ferrum_exception_handler(
    request: Any,  # noqa: ANN401 — Starlette Request signature is structural
    exc: FerrumError,
) -> Any:  # noqa: ANN401 — returns a starlette Response; fastapi is a soft import
    """Translate a :class:`FerrumError` into a JSON HTTP response.

    Returns a ``starlette.responses.JSONResponse`` (imported lazily so core
    Ferrum never imports Starlette). The body contains only sanctioned safe
    fields; DSNs, bound parameter values, and row data never appear.
    """
    from starlette.responses import JSONResponse

    status = map_ferrum_to_http_status(exc)
    payload = _sanitize_ferrum_error_payload(exc)
    return JSONResponse(status_code=status, content={"error": payload})


def register_ferrum_exception_handlers(app: Any) -> None:  # noqa: ANN401
    """Register Ferrum exception handlers on a FastAPI/Starlette app.

    Adds a single handler for :class:`FerrumError` (the base class) so every
    Ferrum subclass is translated through :func:`ferrum_exception_handler`.
    Idempotent: registering twice is a no-op for the second call (FastAPI's
    ``add_exception_handler`` overwrites the prior binding for the same
    exception class).
    """
    app.add_exception_handler(FerrumError, ferrum_exception_handler)


# ---------------------------------------------------------------------------
# Optional fastapi-users adapter
# ---------------------------------------------------------------------------


def _import_fastapi_users_db_protocol() -> Any:  # noqa: ANN401 — soft import
    """Soft-import the ``fastapi_users.db.base.BaseUserDatabase`` protocol.

    Raises:
        ImportError: with a clear, actionable message when ``fastapi-users``
            is not installed. ``pyproject.toml`` does not declare it as a
            dependency (it is the host application's responsibility to pin
            a compatible version); the adapter exists so consumers that
            already use ``fastapi-users`` can swap SQLAlchemy out for Ferrum
            without a new Ferrum-side hard dependency.
    """
    try:
        from fastapi_users.db.base import BaseUserDatabase  # ty: ignore[unresolved-import]
    except ImportError as exc:  # pragma: no cover — exercised by unit test
        raise ImportError(
            "FerrumUserDatabase requires the optional 'fastapi-users' package. "
            "Install it in your application environment; Ferrum does not pin it "
            "(it is not a Ferrum core dependency)."
        ) from exc
    return BaseUserDatabase


class FerrumUserDatabase:
    """``fastapi-users`` ``BaseUserDatabase`` adapter backed by Ferrum models.

    Implements the :class:`fastapi_users.db.base.BaseUserDatabase` protocol
    using Ferrum QuerySet terminals. The host application supplies two
    Pydantic v2 ``ferrum.Model`` subclasses:

    - ``user_model`` — the user table (must expose ``id``, ``email``, and
      ``hashed_password`` plus any custom fields; ``is_active`` /
      ``is_superuser`` / ``is_verified`` are optional booleans).
    - ``oauth_account_model`` — the OAuth account relation table (must
      expose ``id``, ``oauth_name`` (provider), ``access_token``,
      ``expires_at``, ``refresh_token``, ``account_id``, ``account_email``,
      and a foreign-key to the user model named by ``user_fk_field``).

    Each method takes an open Ferrum :class:`~ferrum.connection.Connection`
    (or :class:`~ferrum.connection.Transaction`) so the caller controls
    transaction boundaries — pass a ``Transaction`` to wrap user CRUD in a
    request-scoped transaction.

    Unique-conflict handling: ``create``/``update`` translate
    :class:`FerrumIntegrityError` (e.g. SQLSTATE 23505 unique violation on
    email or OAuth account_id) into ``fastapi_users.exceptions.UserAlreadyExists``
    so the fastapi-users router returns 400 rather than surfacing a 409.

    Soft-imports ``fastapi_users`` at class-construction time so simply
    importing :mod:`ferrum.contrib.fastapi` does not require the extra.
    Constructing :class:`FerrumUserDatabase` without the extra installed
    raises :class:`ImportError`.
    """

    def __init__(
        self,
        user_model: Any,  # noqa: ANN401 — ferrum.Model subclass, typed as Any for .objects access
        oauth_account_model: Any | None = None,  # noqa: ANN401
        *,
        user_fk_field: str = "user_id",
    ) -> None:
        # Trigger the soft import eagerly so misconfiguration fails at
        # construction, not on the first request.
        self._base_protocol = _import_fastapi_users_db_protocol()
        self.user_model = user_model
        self.oauth_account_model = oauth_account_model
        self.user_fk_field = user_fk_field

    async def get_by_id(
        self,
        conn: Connection | Transaction,
        id: Any,  # noqa: ANN401
    ) -> Any:  # noqa: ANN401
        try:
            return await self.user_model.objects.get(conn, id=id)
        except FerrumNotFoundError:
            return None

    async def get_by_email(self, conn: Connection | Transaction, email: str) -> Any:  # noqa: ANN401
        return await self.user_model.objects.filter(email=email).first(conn)

    async def get_by_username(self, conn: Connection | Transaction, username: str) -> Any:  # noqa: ANN401
        # fastapi-users treats username as optional; Ferrum models that do
        # not expose a ``username`` field will raise FerrumCompileError at the
        # QuerySet layer (caught and re-raised as a clear NotImplementedError
        # so the caller sees a single, fastapi-users-shaped error).
        try:
            return await self.user_model.objects.filter(username=username).first(conn)
        except FerrumCompileError as exc:
            raise NotImplementedError(
                "FerrumUserDatabase.get_by_username requires a 'username' field on "
                f"{self.user_model.__name__!r}. Define it or override this method."
            ) from exc

    async def create(self, conn: Connection | Transaction, create_dict: dict[str, Any]) -> Any:  # noqa: ANN401
        try:
            return await self.user_model.objects.create(conn, create_dict)
        except FerrumIntegrityError as exc:
            self._raise_user_already_exists(exc)

    async def update(
        self,
        conn: Connection | Transaction,
        user: Any,  # noqa: ANN401
        update_dict: dict[str, Any],
    ) -> Any:  # noqa: ANN401
        for key, value in update_dict.items():
            setattr(user, key, value)
        try:
            return await self.user_model.objects.update_instance(
                conn, user, fields=list(update_dict.keys())
            )
        except FerrumIntegrityError as exc:
            self._raise_user_already_exists(exc)

    async def delete(
        self,
        conn: Connection | Transaction,
        user: Any,  # noqa: ANN401
    ) -> None:
        await self.user_model.objects.filter(id=user.id).delete(conn)

    async def add_oauth_account(
        self,
        conn: Connection | Transaction,
        user: Any,  # noqa: ANN401
        oauth_account: dict[str, Any],
    ) -> Any:  # noqa: ANN401
        if self.oauth_account_model is None:
            raise NotImplementedError(
                "FerrumUserDatabase.add_oauth_account requires an oauth_account_model."
            )
        oauth_account[self.user_fk_field] = user.id
        try:
            return await self.oauth_account_model.objects.create(conn, oauth_account)
        except FerrumIntegrityError as exc:
            self._raise_user_already_exists(exc)

    async def update_oauth_account(
        self,
        conn: Connection | Transaction,
        user: Any,  # noqa: ANN401
        oauth_name: str,
        update_dict: dict[str, Any],
    ) -> Any:  # noqa: ANN401
        if self.oauth_account_model is None:
            raise NotImplementedError(
                "FerrumUserDatabase.update_oauth_account requires an oauth_account_model."
            )
        account = await self.oauth_account_model.objects.filter(
            **{self.user_fk_field: user.id, "oauth_name": oauth_name}
        ).first(conn)
        if account is None:
            raise FerrumNotFoundError(
                f"OAuth account for provider {oauth_name!r} not found on user "
                f"{user.id!r}. [FERR-Q404]",
            )
        for key, value in update_dict.items():
            setattr(account, key, value)
        return await self.oauth_account_model.objects.update_instance(
            conn, account, fields=list(update_dict.keys())
        )

    async def get_by_oauth_account(
        self,
        conn: Connection | Transaction,
        oauth_name: str,
        account_id: str,
    ) -> Any:  # noqa: ANN401
        if self.oauth_account_model is None:
            raise NotImplementedError(
                "FerrumUserDatabase.get_by_oauth_account requires an oauth_account_model."
            )
        oauth = await self.oauth_account_model.objects.filter(
            oauth_name=oauth_name, account_id=account_id
        ).first(conn)
        if oauth is None:
            return None
        user_id = getattr(oauth, self.user_fk_field)
        return await self.get_by_id(conn, user_id)

    @staticmethod
    def _raise_user_already_exists(exc: FerrumIntegrityError) -> None:
        """Translate a Ferrum unique/integrity error into UserAlreadyExists."""
        try:
            from fastapi_users.exceptions import UserAlreadyExists  # ty: ignore[unresolved-import]
        except ImportError:  # pragma: no cover — exercised by unit test
            # Without fastapi-users installed there is no UserAlreadyExists
            # to raise; re-raise the Ferrum error so the caller still sees
            # the integrity violation.
            raise exc from None
        raise UserAlreadyExists() from exc
