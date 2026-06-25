"""Transaction-scoped PostgreSQL GUC helpers for multi-tenant (RLS) patterns.

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
user-supplied input.
"""

from __future__ import annotations

import contextlib
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
    }
)


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


async def current_setting(
    tx: Transaction, name: str, *, missing_ok: bool = True
) -> str | None:
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
    result = await driver.fetchval(
        f"SELECT current_setting('{name}', $1::boolean)", missing_ok
    )
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
