"""Optional pgvector asyncpg integration helpers.

Register codecs on a connection before reading/writing ``vector`` columns.
This is separate from Ferrum's DDL path and must be invoked explicitly by
application code after ``ferrum.connect()``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ferrum.errors import FerrumConfigError

if TYPE_CHECKING:
    from ferrum.connection import Connection


def _encode_vector(value: list[float]) -> str:
    return "[" + ",".join(str(v) for v in value) + "]"


def _decode_vector(value: str) -> list[float]:
    inner = value.strip("[]")
    if not inner:
        return []
    return [float(part) for part in inner.split(",")]


async def register_vector_codecs(conn: Connection) -> None:
    """Ensure the ``vector`` extension exists and register asyncpg codecs.

    PostgreSQL only — requires ``ferrum-orm[pg]``.
    """
    if conn.dialect != "postgres":
        raise FerrumConfigError(
            "pgvector integration requires a PostgreSQL connection. [FERR-C001]"
        )
    driver = conn._require_driver()
    pool = getattr(driver, "_pool", None)
    if pool is None:
        raise FerrumConfigError("PostgreSQL pool is not open. [FERR-C001]")
    await pool.execute("CREATE EXTENSION IF NOT EXISTS vector")
    await pool.set_type_codec(
        "vector",
        schema="public",
        encoder=_encode_vector,
        decoder=_decode_vector,
        format="text",
    )
