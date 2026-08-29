"""Optional Ferrum extension helpers (pgvector codecs, etc.).

Extension helpers implement the
:class:`~ferrum.drivers.protocol.ConnectionInitializer` protocol so they can
run uniformly on every new pooled connection. See
:class:`ferrum.ext.pgvector.PgVectorInitializer` for the declarative pgvector
entry point.
"""

from ferrum.drivers.protocol import ConnectionInitializer
from ferrum.ext.pgvector import PgVectorInitializer, register_vector_codecs

__all__ = [
    "ConnectionInitializer",
    "PgVectorInitializer",
    "register_vector_codecs",
]
