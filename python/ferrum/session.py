"""Transaction-scoped PostgreSQL GUC and schema helpers for multi-tenant patterns.

Design constraints:
- All set_config calls use transaction-local=true (third arg) so the GUC
  resets automatically when the transaction ends, preventing pool leakage.
- Helpers never accept raw SQL fragments; GUC names and values are always
  bound parameters to asyncpg execute().
- Pool-safety: because GUC state is transaction-scoped, the underlying
  asyncpg connection is safe to return to the pool after commit/rollback.

Security note: GUC name validation (via ALLOWED_GUC_NAMES) prevents injection
through the GUC name position. The GUC value is always a bound parameter — never
interpolated into the SQL string. Callers must not construct GUC names from
user-supplied input. Schema identifiers in ``schema_transaction`` are validated
against a strict allowlist AND an identifier regex — never interpolated from
untrusted input (ratified by AGENTS.md §5a "Schema tenancy and sharding
boundaries").
"""

from __future__ import annotations

import contextlib
import re
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING
from uuid import UUID

from ferrum.errors import FerrumCompileError

if TYPE_CHECKING:
    from ferrum.connection import Connection, Transaction

# Allowlisted GUC names. Reject anything not in this set with FerrumCompileError.
# Extend this set when new safe GUC names are needed. Do not accept GUC names
# from user-supplied input — they must come from trusted application code.
ALLOWED_GUC_NAMES: frozenset[str] = frozenset(
    {
        "app.team_id",
        "app.platform_admin",
        "ferrum.tenant_id",  # generic alias for ORM users
        "ferrum.admin",  # generic alias
        "statement_timeout",
        "lock_timeout",
        "work_mem",
        "application_name",
        "search_path",  # set transaction-local by schema_transaction()
    }
)

# Admin GUCs that may be set by ``platform_admin_transaction``. A strict subset
# of ``ALLOWED_GUC_NAMES`` containing only flags that activate RLS bypass /
# platform-admin policies. Tenant-id GUCs are intentionally excluded so the
# admin path never needs a fake tenant id (AGENTS.md §5a).
ALLOWED_ADMIN_GUC_NAMES: frozenset[str] = frozenset(
    {
        "app.platform_admin",
        "ferrum.admin",
    }
)

# Allowlisted schema names for ``schema_transaction``. Callers register tenant
# schemas here (or pass ``allowed_schemas=`` at the call site) so a schema name
# is never interpolated from untrusted input. The identifier regex
# (``_SCHEMA_IDENT_RE``) is ALSO enforced, so only safe identifiers can ever
# reach ``SET LOCAL search_path``. Defaults to ``{"public"}``.
ALLOWED_SCHEMA_NAMES: frozenset[str] = frozenset({"public"})

# Strict PostgreSQL identifier pattern for schema names: start with a letter or
# underscore, then letters/digits/underscores, max 63 chars (PostgreSQL limit).
# Mirrors the identifier rule used by ``call_function`` in ``connection.py``;
# duplicated here so ``session.py`` stays self-contained and does not import a
# private symbol from ``connection.py``.
_SCHEMA_IDENT_RE: re.Pattern[str] = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,62}$")


def _validate_guc_name(name: str) -> None:
    """Raise FerrumCompileError if name is not in the GUC allowlist.

    Args:
        name: The GUC parameter name to validate.

    Raises:
        FerrumCompileError: If ``name`` is not in ``ALLOWED_GUC_NAMES``.
    """
    if name not in ALLOWED_GUC_NAMES:
        allowed = ", ".join(sorted(ALLOWED_GUC_NAMES))
        raise FerrumCompileError(
            f"GUC name {name!r} is not in the Ferrum session allowlist. "
            f"Allowed names: {allowed}. [FERR-C102]",
            category="guc_name_not_allowed",
        )


async def set_config(tx: Transaction, name: str, value: str) -> None:
    """SET LOCAL config within a transaction. name must be in the GUC allowlist.

    Uses ``set_config(name, value, true)`` — the ``transaction_local=true`` third
    argument ensures the GUC resets when the transaction ends, so pooled connections
    never leak tenant state across requests.

    Args:
        tx: A transaction-scoped handle from ``conn.transaction()``.
        name: GUC name from ``ALLOWED_GUC_NAMES``. Rejected otherwise.
        value: The string value to set. Passed as a bound parameter — never interpolated.

    Raises:
        FerrumCompileError: If ``name`` is not in ``ALLOWED_GUC_NAMES``.
    """
    _validate_guc_name(name)
    driver = tx._require_driver()
    # GUC name is allowlist-validated above — not user input.
    # Value is a bound parameter ($1) — never interpolated into SQL.
    await driver.execute(f"SELECT set_config('{name}', $1, true)", value)


async def current_setting(tx: Transaction, name: str, *, missing_ok: bool = True) -> str | None:
    """Read a GUC value from the current transaction context.

    Args:
        tx: A transaction-scoped handle from ``conn.transaction()``.
        name: GUC name from ``ALLOWED_GUC_NAMES``. Rejected otherwise.
        missing_ok: When ``True`` (default), returns ``None`` if the setting is not
            present rather than raising a PostgreSQL error. When ``False``,
            PostgreSQL raises an error for unset settings.

    Returns:
        The string value of the setting, or ``None`` when ``missing_ok=True`` and
        the setting is not present or is empty.

    Raises:
        FerrumCompileError: If ``name`` is not in ``ALLOWED_GUC_NAMES``.
    """
    _validate_guc_name(name)
    driver = tx._require_driver()
    # GUC name is allowlist-validated above.
    # missing_ok is a bool bound parameter — not user-controlled SQL.
    result = await driver.fetchval(f"SELECT current_setting('{name}', $1::boolean)", missing_ok)
    if result is None or result == "":
        return None
    return str(result)


@contextlib.asynccontextmanager
async def tenant_transaction(
    conn: Connection,
    tenant_id: str | UUID,
    *,
    guc_name: str = "app.team_id",
    admin: bool = False,
    admin_guc: str = "app.platform_admin",
    isolation: str | None = None,
    readonly: bool = False,
) -> AsyncIterator[Transaction]:
    """Open a transaction and bind tenant GUC before yielding.

    Admin mode additionally sets ``admin_guc = 'true'`` (for RLS bypass policies).
    GUC state is transaction-local so the underlying pooled connection is always
    returned in a clean state after commit or rollback.

    Args:
        conn: An open Ferrum :class:`~ferrum.connection.Connection`.
        tenant_id: The tenant identifier (UUID or string) to bind via GUC.
        guc_name: Which GUC to set for tenant isolation (default: ``"app.team_id"``).
            Must be in ``ALLOWED_GUC_NAMES``.
        admin: If ``True``, also sets ``admin_guc = 'true'`` to activate RLS bypass
            policies for platform-admin operations.
        admin_guc: GUC name for the platform-admin flag
            (default: ``"app.platform_admin"``). Validated only when ``admin=True``.
            Must be in ``ALLOWED_GUC_NAMES``.
        isolation: Transaction isolation level passed to
            :meth:`~ferrum.connection.Connection.transaction`, or ``None`` for the
            server default.
        readonly: Open the transaction in READ ONLY mode.

    Yields:
        A :class:`~ferrum.connection.Transaction` with the tenant GUC bound before
        the first ``yield`` and automatically reset on commit or rollback.

    Raises:
        FerrumCompileError: If ``guc_name`` or (when ``admin=True``) ``admin_guc``
            is not in ``ALLOWED_GUC_NAMES``.

    Example::

        async with ferrum.session.tenant_transaction(conn, team_id) as tx:
            rows = await MyModel.objects.filter(...).all(tx)

        # Admin path — also sets app.platform_admin = 'true':
        async with ferrum.session.tenant_transaction(conn, team_id, admin=True) as tx:
            rows = await SecureModel.objects.all(tx)
    """
    # Validate allowlist up-front so we fail before opening the transaction.
    _validate_guc_name(guc_name)
    if admin:
        _validate_guc_name(admin_guc)

    async with conn.transaction(isolation=isolation, readonly=readonly) as tx:
        await set_config(tx, guc_name, str(tenant_id))
        if admin:
            await set_config(tx, admin_guc, "true")
        yield tx


def _validate_admin_guc(name: str) -> None:
    """Raise FerrumCompileError if ``name`` is not in the admin GUC allowlist.

    The admin allowlist is a strict subset of ``ALLOWED_GUC_NAMES`` containing
    only RLS-bypass / platform-admin flags — never tenant-id GUCs.
    """
    if name not in ALLOWED_ADMIN_GUC_NAMES:
        allowed = ", ".join(sorted(ALLOWED_ADMIN_GUC_NAMES))
        raise FerrumCompileError(
            f"Admin GUC {name!r} is not in the Ferrum admin allowlist. "
            f"Allowed admin GUCs: {allowed}. [FERR-C102]",
            category="guc_name_not_allowed",
        )


def _validate_schema_name(schema: str, allowed: frozenset[str]) -> None:
    """Validate a schema identifier against the regex AND the allowlist.

    The regex prevents SQL injection via the identifier; the allowlist
    restricts the set of permitted schemas. Both must pass.
    """
    if not _SCHEMA_IDENT_RE.match(schema):
        raise FerrumCompileError(
            f"Invalid schema identifier: {schema!r}. Schema names must start "
            "with a letter or underscore, contain only letters, digits, and "
            "underscores, and be at most 63 characters. Do not construct schema "
            "names from user-supplied input. [FERR-C102]",
            category="invalid_identifier",
        )
    if schema not in allowed:
        permitted = ", ".join(sorted(allowed))
        raise FerrumCompileError(
            f"Schema {schema!r} is not in the Ferrum schema allowlist. "
            f"Allowed schemas: {permitted}. Register tenant schemas in "
            "ALLOWED_SCHEMA_NAMES or pass allowed_schemas= at the call site. "
            "[FERR-C102]",
            category="schema_not_allowed",
        )


@contextlib.asynccontextmanager
async def platform_admin_transaction(
    conn: Connection,
    *,
    admin_guc: str = "app.platform_admin",
    isolation: str | None = None,
    readonly: bool = False,
) -> AsyncIterator[Transaction]:
    """Open a transaction and bind ONLY an admin-bypass GUC — no fake tenant id.

    This is the dedicated platform-admin path ratified by AGENTS.md §5a. It
    sets a single allowlisted admin flag (e.g. ``app.platform_admin = 'true'``)
    so RLS bypass policies activate for cross-tenant platform operations, and
    does NOT set a tenant-id GUC. GUC state is transaction-local
    (``set_config(..., true)``) so the underlying pooled connection is always
    returned in a clean state after commit or rollback, including on
    cancellation.

    Args:
        conn: An open Ferrum :class:`~ferrum.connection.Connection`.
        admin_guc: GUC name for the platform-admin flag
            (default: ``"app.platform_admin"``). Must be in
            ``ALLOWED_ADMIN_GUC_NAMES`` (a strict subset of
            ``ALLOWED_GUC_NAMES``).
        isolation: Transaction isolation level passed to
            :meth:`~ferrum.connection.Connection.transaction`, or ``None`` for
            the server default.
        readonly: Open the transaction in READ ONLY mode.

    Yields:
        A :class:`~ferrum.connection.Transaction` with the admin GUC bound
        before the first ``yield`` and automatically reset on commit/rollback.

    Raises:
        FerrumCompileError: If ``admin_guc`` is not in ``ALLOWED_ADMIN_GUC_NAMES``.

    Example::

        async with ferrum.session.platform_admin_transaction(conn) as tx:
            rows = await SecureModel.objects.all(tx)
    """
    # Validate the admin allowlist up-front so we fail before opening the tx.
    _validate_admin_guc(admin_guc)

    async with conn.transaction(isolation=isolation, readonly=readonly) as tx:
        await set_config(tx, admin_guc, "true")
        yield tx


@contextlib.asynccontextmanager
async def schema_transaction(
    conn: Connection,
    schema: str,
    *,
    allowed_schemas: frozenset[str] | None = None,
    isolation: str | None = None,
    readonly: bool = False,
) -> AsyncIterator[Transaction]:
    """Open a transaction and set a transaction-local ``search_path``.

    The schema identifier is validated against a strict allowlist AND an
    identifier regex (``^[a-zA-Z_][a-zA-Z0-9_]{0,62}$``) — it is never
    string-interpolated from untrusted input (AGENTS.md §5a). The
    ``search_path`` is bound via ``set_config('search_path', schema, true)``
    so it resets automatically on commit/rollback (and on cancellation),
    guaranteeing no `search_path` leakage onto a pooled connection.

    Args:
        conn: An open Ferrum :class:`~ferrum.connection.Connection`.
        schema: The schema name to set as the transaction-local
            ``search_path``. Must pass the identifier regex AND be in the
            schema allowlist.
        allowed_schemas: Optional per-call allowlist. When ``None`` (default),
            the module-level ``ALLOWED_SCHEMA_NAMES`` is used. Register tenant
            schemas there at application startup, or pass an explicit set here.
        isolation: Transaction isolation level passed to
            :meth:`~ferrum.connection.Connection.transaction`, or ``None`` for
            the server default.
        readonly: Open the transaction in READ ONLY mode.

    Yields:
        A :class:`~ferrum.connection.Transaction` with the transaction-local
        ``search_path`` bound before the first ``yield``.

    Raises:
        FerrumCompileError: If ``schema`` fails the identifier regex or is not
            in the schema allowlist.

    Example::

        async with ferrum.session.schema_transaction(conn, "tenant_a") as tx:
            rows = await MyModel.objects.all(tx)
    """
    # Validate both the regex and the allowlist up-front so we fail before
    # opening the transaction (no wasted round-trip, no partial state).
    allow = allowed_schemas if allowed_schemas is not None else ALLOWED_SCHEMA_NAMES
    _validate_schema_name(schema, allow)

    async with conn.transaction(isolation=isolation, readonly=readonly) as tx:
        await set_config(tx, "search_path", schema)
        yield tx
