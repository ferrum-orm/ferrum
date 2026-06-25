"""Optional pgvector asyncpg integration helpers.

Register codecs on a connection before reading/writing ``vector`` columns.
This is separate from Ferrum's DDL path and must be invoked explicitly by
application code after ``ferrum.connect()``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ferrum.errors import FerrumCompileError, FerrumConfigError

if TYPE_CHECKING:
    from ferrum.connection import Connection
    from ferrum.models import Model

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


def _encode_vector(value: list[float]) -> str:
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

    Idempotent: safe to call multiple times and from concurrent startup paths.
    ``DuplicateObjectError`` from the extension-creation step and codec
    re-registration on an already-configured pool are both handled gracefully.

    Args:
        conn: An open Ferrum ``Connection``.
        timeout: Statement timeout (seconds) for the ``CREATE EXTENSION`` DDL.
            Defaults to 5 s.  Set to ``0`` to disable the timeout guard.

    Raises:
        FerrumConfigError: If the connection is not a PostgreSQL connection or
            the pool is not open.
    """
    if conn.dialect != "postgres":
        raise FerrumConfigError(
            "pgvector integration requires a PostgreSQL connection. [FERR-C001]"
        )
    driver = conn._require_driver()
    pool = getattr(driver, "_pool", None)
    # TimedQueryExecutor wraps the real driver; fall back to its _inner attribute.
    if pool is None:
        inner = getattr(driver, "_inner", None)
        pool = getattr(inner, "_pool", None) if inner is not None else None
    if pool is None:
        raise FerrumConfigError("PostgreSQL pool is not open. [FERR-C001]")

    # CREATE EXTENSION — idempotent via IF NOT EXISTS; the DuplicateObjectError
    # guard covers rare race conditions where two concurrent startup paths both
    # attempt the DDL at the same moment.
    try:
        create_sql = "CREATE EXTENSION IF NOT EXISTS vector"
        if timeout > 0:
            create_sql = f"SET LOCAL statement_timeout = {int(timeout * 1000)}; {create_sql}"
        await pool.execute("CREATE EXTENSION IF NOT EXISTS vector")
    except Exception as exc:
        # asyncpg raises DuplicateObjectError (SQLSTATE 42710) if another
        # concurrent caller committed the extension between our IF NOT EXISTS
        # check and our DDL execution.  Treat it as success.
        exc_name = type(exc).__name__
        if "DuplicateObject" not in exc_name:
            raise

    # Codec registration — asyncpg raises InvalidStateError if the same custom
    # type is registered twice on the same pool.  Treat it as idempotent.
    try:
        await pool.set_type_codec(
            "vector",
            schema="public",
            encoder=_encode_vector,
            decoder=_decode_vector,
            format="text",
        )
    except Exception as exc:
        exc_name = type(exc).__name__
        if exc_name not in ("InvalidStateError", "AlreadyInitializedError"):
            # Re-raise unknown errors; swallow the known re-registration ones.
            raise


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
        raise FerrumConfigError(
            "vector_search requires a PostgreSQL connection. [FERR-C001]"
        )

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
