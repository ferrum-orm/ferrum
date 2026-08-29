"""Optional pgvector asyncpg integration helpers.

Register codecs on a connection before reading/writing ``vector`` columns.
This is separate from Ferrum's DDL path and must be invoked explicitly by
application code after ``ferrum.connect()``.

Two equivalent entry points:

- :func:`register_vector_codecs` — the original one-call helper. Builds a
  :class:`PgVectorInitializer` and runs it.
- :class:`PgVectorInitializer` — the declarative
  :class:`~ferrum.drivers.protocol.ConnectionInitializer` implementation.
  Consumer code that wants to compose initializers (pgvector + citext + a
  custom codec, for example) instantiates one of each and runs them in
  sequence against the open ``Connection``::

      async with ferrum.connect(dsn) as conn:
          await PgVectorInitializer().initialize(conn)
          await CitextInitializer().initialize(conn)

  Future ``connect(..., extensions=[pgvector])`` plumbing (not yet wired
  through ``ferrum.connect``) will accept a list of ``ConnectionInitializer``
  objects and run them on every new pooled connection from the pool
  ``init`` hook, so the registration survives pool growth and failover
  without application code calling ``register_vector_codecs`` again.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ferrum.drivers.protocol import ConnectionLike
from ferrum.errors import FerrumCompileError, FerrumConfigError

if TYPE_CHECKING:
    from ferrum.connection import Connection
    from ferrum.models import Model

# Re-export the protocol under the pgvector namespace for IDE discovery.
# ``ConnectionLike`` is a structural alias (``Any``); the explicit aliasing
# keeps the public pgvector surface self-documenting without importing
# ``ferrum.connection`` here (preserves the import boundary).
__all__ = [
    "PgVectorInitializer",
    "register_vector_codecs",
    "vector_search",
]

# Metric name → (distance operator, score expression template)
# The template placeholder ``{field}`` is replaced with the quoted column name
# (metadata-sourced, never user input).  ``$1`` is the bound vector parameter.
_METRIC_OPS: dict[str, tuple[str, str]] = {
    # cosine distance: score = 1 - distance (0 = identical, 1 = orthogonal)
    "cosine": ("<=>", "1 - ({field} <=> $1::vector)"),
    # Euclidean distance: normalised to (0, 1] via 1/(1+d)
    "l2": ("<->", "1 / (1 + ({field} <-> $1::vector))"),
    # Negative inner product (pgvector stores negated inner product)
    # score = -distance  → positive when vectors are aligned
    "inner_product": ("<#>", "-({field} <#> $1::vector)"),
}

_VALID_METRICS: frozenset[str] = frozenset(_METRIC_OPS)


def _encode_vector(value: list[float] | str) -> str:
    """Encode a float list as a pgvector text literal.

    Idempotent for an already-encoded ``"[f,f,...]"`` string. Ferrum's
    ``nearest_to()`` / ``vector_search()`` bind a text literal plus
    ``$N::vector``; when this codec is registered, asyncpg still runs the
    encoder on that string — re-iterating characters would produce garbage
    like ``"[[,-,0,.,1,...]]"`` and Postgres raises
    ``InvalidTextRepresentationError``.
    """
    if isinstance(value, str):
        # Already a pgvector text literal (or opaque text the server will cast).
        return value
    return "[" + ",".join(str(v) for v in value) + "]"


def _decode_vector(value: str) -> list[float]:
    inner = value.strip("[]")
    if not inner:
        return []
    return [float(part) for part in inner.split(",")]


async def register_vector_codecs(
    conn: Connection,
    *,
    timeout: float = 5.0,
) -> None:
    """Ensure the ``vector`` extension exists and register asyncpg codecs.

    Convenience wrapper around :class:`PgVectorInitializer`. Builds an
    initializer with the supplied ``timeout`` and runs it against ``conn``.
    The codec is installed pool-wide (every current and future connection),
    so ``vector`` columns decode to ``list[float]`` no matter which pooled
    connection serves a query.

    Writing a vector does not require this call: Ferrum binds vector values as
    pgvector text literals, which asyncpg accepts without a codec.

    Idempotent: safe to call multiple times and from concurrent startup paths.
    ``DuplicateObjectError`` from the extension-creation step and repeat
    registration of the same codec are both handled gracefully.

    Args:
        conn: An open Ferrum ``Connection``.
        timeout: Timeout (seconds) for the ``CREATE EXTENSION`` DDL.
            Defaults to 5 s.  Set to ``0`` to disable the timeout guard.

    Raises:
        FerrumConfigError: If the connection is not a PostgreSQL connection, the
            pool is not open, or the driver is not the asyncpg driver.
    """
    await PgVectorInitializer(timeout=timeout).initialize(conn)


class PgVectorInitializer:
    """Declarative :class:`ConnectionInitializer` for the pgvector extension.

    Running ``initialize(conn)``:

    1. Validates the connection is PostgreSQL and the asyncpg pool is open.
    2. Runs ``CREATE EXTENSION IF NOT EXISTS vector`` directly against the
       asyncpg pool (``conn._driver._pool.execute(...)``), preserving the
       behavior of the legacy ``register_vector_codecs`` helper.
       ``DuplicateObjectError`` (SQLSTATE 42710) from a concurrent startup
       path is tolerated.
    3. Registers the ``vector`` text codec pool-wide via
       ``driver.add_type_codec(...)`` so every current and future pooled
       connection decodes ``vector`` columns to ``list[float]``.

    The initializer is idempotent. Re-running it on an already-prepared
    connection is a no-op (the codec registration deduplicates inside
    ``add_type_codec``; ``CREATE EXTENSION IF NOT EXISTS`` is itself
    idempotent).

    Fail-closed: if the ``vector`` contrib package is not installed on the
    server, ``CREATE EXTENSION`` raises and ``initialize`` propagates the
    mapped Ferrum error. A pool that silently served queries against
    unregistered vector columns would produce non-deterministic ``DataError``
    depending on which pooled connection served the query.

    Args:
        timeout: Timeout (seconds) for the ``CREATE EXTENSION`` DDL.
            Defaults to 5 s. Set to ``0`` to disable the timeout guard.

    Example:
        ::

            async with ferrum.connect(dsn) as conn:
                await PgVectorInitializer().initialize(conn)
                # vector columns now decode to list[float] on every pooled
                # connection, including ones the pool opens later.
    """

    name = "pgvector"

    def __init__(self, *, timeout: float = 5.0) -> None:
        self._timeout = timeout

    async def initialize(self, conn: ConnectionLike) -> None:
        """Run the pgvector extension setup and codec registration on ``conn``.

        Implements the :class:`~ferrum.drivers.protocol.ConnectionInitializer`
        protocol. See the class docstring for the contract.
        """
        if conn.dialect != "postgres":
            raise FerrumConfigError(
                "pgvector integration requires a PostgreSQL connection. [FERR-C001]"
            )
        driver = conn._driver
        pool = getattr(driver, "_pool", None)
        if driver is None or pool is None:
            raise FerrumConfigError("PostgreSQL pool is not open. [FERR-C001]")

        # CREATE EXTENSION — idempotent via IF NOT EXISTS; the
        # DuplicateObjectError guard covers rare race conditions where two
        # concurrent startup paths both attempt the DDL at the same moment.
        # Issued against the pool's init-time execute path (the same path
        # ``AsyncpgDriver._init_conn`` would use if the initializer were
        # wired through the pool ``init`` hook). The timeout guards a slow
        # ``CREATE EXTENSION`` against an unhealthy server.
        timeout = self._timeout
        try:
            await pool.execute(
                "CREATE EXTENSION IF NOT EXISTS vector",
                timeout=timeout if timeout > 0 else None,
            )
        except Exception as exc:
            # asyncpg raises DuplicateObjectError (SQLSTATE 42710) if another
            # concurrent caller committed the extension between our IF NOT
            # EXISTS check and our DDL execution.  Treat it as success.
            exc_name = type(exc).__name__
            if "DuplicateObject" not in exc_name:
                raise

        # Registering through the driver applies the codec from the pool's
        # ``init`` hook, so every connection — including ones the pool opens
        # later — decodes ``vector`` columns to ``list[float]``. Registering
        # against a single acquired connection instead made vector reads
        # depend on which pooled connection served the query.
        add_codec = getattr(driver, "add_type_codec", None)
        if add_codec is None:
            raise FerrumConfigError("pgvector integration requires the asyncpg driver. [FERR-C001]")
        await add_codec(
            "vector",
            schema="public",
            encoder=_encode_vector,
            decoder=_decode_vector,
            format="text",
        )


async def vector_search(
    conn: Connection,
    model: type[Model],
    field: str,
    query_vector: list[float],
    *,
    metric: str = "cosine",
    limit: int = 10,
    score_alias: str = "score",
    filters: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Return rows with a computed similarity score column.

    Executes::

        SELECT *, <score_expr> AS <score_alias>
        FROM <table>
        [WHERE <field> IS NOT NULL [AND <filter_col> = $N ...]]
        ORDER BY <field> <op> $1::vector
        LIMIT $2

    All SQL identifiers (table name, field name, filter column names) come from
    model ``ModelMetadata`` allowlists, never from user-supplied strings.
    All values (query vector, limit, filter values) travel as bound parameters.

    Args:
        conn: An open Ferrum ``Connection``.
        model: The Ferrum ``Model`` class to query.
        field: Name of the ``Vector`` field on the model.
        query_vector: The query embedding as a list of floats.
        metric: Distance metric — ``"cosine"``, ``"l2"``, or
            ``"inner_product"``.  Defaults to ``"cosine"``.
        limit: Maximum number of rows to return.  Defaults to 10.
        score_alias: Column name for the computed score in the result dicts.
            Defaults to ``"score"``.
        filters: Optional equality filter dict.  Keys must be valid field names
            on the model; values become bound parameters.  No Q-object support
            in this version.

    Returns:
        A list of dicts containing all model columns plus the key named by
        ``score_alias``.

    Raises:
        FerrumCompileError: If ``field`` is not a known vector field on
            ``model``, if ``metric`` is not one of the supported values, or if
            a filter key is not a known field on ``model``.
        FerrumConfigError: If the connection is not a PostgreSQL connection.
    """
    if conn.dialect != "postgres":
        raise FerrumConfigError("vector_search requires a PostgreSQL connection. [FERR-C001]")

    if metric not in _VALID_METRICS:
        raise FerrumCompileError(
            f"Unknown vector metric {metric!r}. "
            f"Supported values: {sorted(_VALID_METRICS)}. [FERR-C102]",
            model=model.__name__,
            operator=metric,
            category="unknown_metric",
        )

    meta = model.get_metadata()
    # Build field name → FieldMeta index for O(1) lookups.
    field_by_name = {f.name: f for f in meta.fields}

    # Validate the vector field.
    if field not in field_by_name:
        raise FerrumCompileError(
            f"Unknown field {field!r} on model {model.__name__!r}. [FERR-C102]",
            model=model.__name__,
            field=field,
            category="unknown_field",
        )
    field_meta = field_by_name[field]
    if field_meta.field_type != "vector":
        raise FerrumCompileError(
            f"Field {field!r} on model {model.__name__!r} is not a vector field "
            f"(field_type={field_meta.field_type!r}). [FERR-C102]",
            model=model.__name__,
            field=field,
            category="non_vector_field",
        )

    # Validate filter keys.
    filters = filters or {}
    for fk in filters:
        if fk not in field_by_name:
            raise FerrumCompileError(
                f"Unknown filter field {fk!r} on model {model.__name__!r}. [FERR-C102]",
                model=model.__name__,
                field=fk,
                category="unknown_field",
            )

    # All identifiers sourced from metadata (never user input).
    table = meta.table_name
    col = field_meta.column_name

    dist_op, score_tmpl = _METRIC_OPS[metric]
    score_expr = score_tmpl.format(field=f'"{col}"')

    # Build bound params list:
    #   $1 = query_vector (cast to ::vector in the SQL)
    #   $2 = limit
    #   $3..N = filter values
    params: list[Any] = [_encode_vector(query_vector), limit]
    where_clauses: list[str] = [f'"{col}" IS NOT NULL']

    for fk, fv in filters.items():
        param_idx = len(params) + 1  # 1-based
        col_name = field_by_name[fk].column_name
        where_clauses.append(f'"{col_name}" = ${param_idx}')
        params.append(fv)

    where_sql = " AND ".join(where_clauses)

    sql = (
        f'SELECT *, {score_expr} AS "{score_alias}" '
        f'FROM "{table}" '
        f"WHERE {where_sql} "
        f'ORDER BY "{col}" {dist_op} $1::vector '
        f"LIMIT $2"
    )

    driver = conn._require_driver()
    rows = await driver.fetch(sql, *params)

    return [dict(row) for row in rows]
