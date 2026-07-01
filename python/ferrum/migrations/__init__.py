"""Ferrum migration subsystem.

Submodule responsibilities:
- ``orchestrator``: dry-run, classification, apply sequencing.
- ``ledger``: migration history table access (append-only).
- ``tokens``: confirmation-token emit/validate (no secrets stored).
- ``gates``: destructive + non-dev confirmation guards.

Security invariants (MIG-1 through MIG-8):
- Dry-run is mandatory before apply.
- Destructive actions (column drop, table drop, type narrowing, NOT NULL on
  populated column) require explicit confirmation.
- Non-development applies require environment confirmation.
- Confirmation tokens are never emitted to argv or public logs.
- Token replay after apply is rejected.

Public API (stable):
- ``apply(conn, plan_json, ...)`` — apply or dry-run a Rust-generated plan JSON.
- ``MigrationResult`` — return value of ``apply()``.
- ``_op_to_sql`` — convert a plan operation dict to DDL SQL (internal but testable).
"""

import ferrum.migrations.loader as loader
import ferrum.migrations.operations as operations
from ferrum.migrations.base import Migration
from ferrum.migrations.operations import (
    CreateExtension,
    CreateFullTextCatalog,
    CreateFullTextIndex,
    CreateFunction,
    CreatePolicy,
    DisableRLS,
    DropExtension,
    DropFullTextIndex,
    DropFunction,
    DropPolicy,
    EnableRLS,
)
from ferrum.migrations.orchestrator import MigrationResult, _op_to_sql, apply, compute_plan

__all__ = [
    "CreateExtension",
    "CreateFullTextCatalog",
    "CreateFullTextIndex",
    "CreateFunction",
    "CreatePolicy",
    "DisableRLS",
    "DropExtension",
    "DropFullTextIndex",
    "DropFunction",
    "DropPolicy",
    "EnableRLS",
    "Migration",
    "MigrationResult",
    "_op_to_sql",
    "apply",
    "compute_plan",
    "loader",
    "operations",
]
