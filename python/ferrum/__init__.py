"""Ferrum — async ORM for Python with a Rust-powered core.

Public re-exports for the top-level ``ferrum`` namespace.
Import paths are stable API; internal module paths are not.
"""

from __future__ import annotations

__version__ = "0.1.2"
__all__ = [
    "FerrumCompileError",
    "FerrumConfigError",
    "FerrumConnectionError",
    "FerrumDatabaseError",
    "FerrumError",
    "FerrumIntegrityError",
    "FerrumMigrationError",
    "FerrumMultipleObjectsError",
    "FerrumNotFoundError",
    "FerrumSchemaError",
    "Field",
    "ForeignKey",
    "Index",
    "ManyToMany",
    "MigrationResult",
    "Model",
    "ModelConfig",
    "OneToOne",
    "QuerySet",
    "TSVector",
    "Transaction",
    "Vector",
    "clear_hooks",
    "connect",
    "contrib",
    "register_hook",
]

from ferrum import contrib
from ferrum.connection import Transaction, connect
from ferrum.errors import (
    FerrumCompileError,
    FerrumConfigError,
    FerrumConnectionError,
    FerrumDatabaseError,
    FerrumError,
    FerrumIntegrityError,
    FerrumMigrationError,
    FerrumMultipleObjectsError,
    FerrumNotFoundError,
    FerrumSchemaError,
)
from ferrum.hooks import clear_hooks, register_hook
from ferrum.migrations import MigrationResult
from ferrum.models import (
    Field,
    ForeignKey,
    Index,
    ManyToMany,
    Model,
    ModelConfig,
    OneToOne,
    TSVector,
    Vector,
)
from ferrum.queryset import QuerySet
