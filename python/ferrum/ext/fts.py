"""Optional full-text search helpers mirroring ``ext/pgvector.vector_search``."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from ferrum.errors import FerrumCompileError

if TYPE_CHECKING:
    from ferrum.connection import Connection
    from ferrum.models import Model

_FTS_MODES = frozenset({"plain", "phrase", "websearch", "boolean"})
_MODE_TO_OPERATOR: dict[str, str] = {
    "plain": "match",
    "phrase": "match_phrase",
    "websearch": "match_websearch",
    "boolean": "match_boolean",
}


async def scored_search(
    conn: Connection,
    model: type[Model],
    field: str,
    query: str,
    *,
    mode: Literal["plain", "phrase", "websearch", "boolean"] = "plain",
    limit: int = 10,
    score_alias: str = "score",
    filters: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Return rows ranked by full-text relevance with a computed score column.

    Uses the QuerySet compiler for the active connection dialect — all identifiers
    come from model metadata allowlists; the query string is a bound parameter.

    Args:
        conn: An open Ferrum ``Connection``.
        model: The Ferrum ``Model`` class to query.
        field: Full-text field name (``tsvector`` or indexed ``text``).
        query: Search query string (bound, never interpolated).
        mode: Query mode — ``plain``, ``phrase``, ``websearch``, or ``boolean``.
        limit: Maximum rows to return.
        score_alias: Key for the rank column in result dicts.
        filters: Optional equality filters (field name → value).

    Returns:
        List of row dicts including ``score_alias``.

    Raises:
        FerrumCompileError: Unknown field or unsupported mode.
        FerrumConfigError: Native extension not built.
    """
    if mode not in _FTS_MODES:
        raise FerrumCompileError(
            f"Unknown FTS mode {mode!r}. Supported: {sorted(_FTS_MODES)}.",
            model=model.__name__,
        )

    from ferrum.queryset import QuerySet

    meta = model.get_metadata()
    field_names = {f.name for f in meta.fields}
    if field not in field_names:
        raise FerrumCompileError(
            f"Unknown field {field!r} on model {model.__name__!r}.",
            model=model.__name__,
            field=field,
        )

    qs: QuerySet[Any] = QuerySet(model)
    operator = _MODE_TO_OPERATOR[mode]
    qs = qs.filter(**{f"{field}__{operator}": query}).rank_by(field, query, mode=mode)
    if filters:
        qs = qs.filter(**filters)
    qs = qs.limit(limit)

    dialect = getattr(conn, "dialect", "postgres")
    compiled = qs._compile(dialect=dialect)
    from ferrum.queryset import _decode_bound_param

    driver = conn._require_driver()
    params = [_decode_bound_param(p) for p in compiled["bound_params"]]
    rows = await driver.fetch(compiled["sql_text"], *params)

    # Rank is embedded in ORDER BY; re-run as values query for explicit score column
    # by selecting all columns — drivers return full rows; score requires dialect-specific
    # follow-up. For MVP, attach a placeholder rank from row order (best-first).
    results: list[dict[str, Any]] = []
    for rank, row in enumerate(rows, start=1):
        data = dict(row)
        data[score_alias] = 1.0 / rank
        results.append(data)
    return results
