"""Ferrum — async ORM for Python with a Rust-powered core.

Public re-exports for the top-level ``ferrum`` namespace.
Import paths are stable API; internal module paths are not.
"""

from __future__ import annotations

__version__ = "0.1.3"
__all__ = [
    "CreateExtension",
    "CreateFunction",
    "CreatePolicy",
    "DisableRLS",
    "DropExtension",
    "DropFunction",
    "DropPolicy",
    "EnableRLS",
    "FerrumCompileError",
    "FerrumConfigError",
    "FerrumConnectionError",
    "FerrumDatabaseError",
    "FerrumDeferredFieldError",
    "FerrumError",
    "FerrumIntegrityError",
    "FerrumMigrationError",
    "FerrumMultipleObjectsError",
    "FerrumNotFoundError",
    "FerrumRelationNotLoadedError",
    "FerrumSchemaError",
    "FerrumTimeoutError",
    "Field",
    "ForeignKey",
    "Index",
    "ManyToMany",
    "MigrationResult",
    "Model",
    "ModelConfig",
    "OneToOne",
    "Q",
    "QuerySet",
    "RetryPolicy",
    "TSVector",
    "Transaction",
    "Vector",
    "clear_hooks",
    "connect",
    "contrib",
    "enable_metrics",
    "enable_opentelemetry",
    "get_metrics",
    "get_session_config",
    "observability",
    "register_hook",
    "session",
    "set_session_config",
    "tenant_transaction",
]

from ferrum import contrib, observability, session
from ferrum.connection import Transaction, connect
from ferrum.errors import (
    FerrumCompileError,
    FerrumConfigError,
    FerrumConnectionError,
    FerrumDatabaseError,
    FerrumDeferredFieldError,
    FerrumError,
    FerrumIntegrityError,
    FerrumMigrationError,
    FerrumMultipleObjectsError,
    FerrumNotFoundError,
    FerrumRelationNotLoadedError,
    FerrumSchemaError,
    FerrumTimeoutError,
)
from ferrum.expressions import Q
from ferrum.hooks import clear_hooks, register_hook
from ferrum.migrations import (
    CreateExtension,
    CreateFunction,
    CreatePolicy,
    DisableRLS,
    DropExtension,
    DropFunction,
    DropPolicy,
    EnableRLS,
    MigrationResult,
)
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
from ferrum.observability import enable_metrics, enable_opentelemetry, get_metrics
from ferrum.queryset import QuerySet
from ferrum.runtime import RetryPolicy
from ferrum.session import (
    current_setting as get_session_config,
)
from ferrum.session import (
    set_config as set_session_config,
)
from ferrum.session import (
    tenant_transaction,
)
