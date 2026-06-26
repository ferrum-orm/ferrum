//! Structured error types for the Ferrum core engine.
//!
//! All errors carry structured fields (model name, field name, operator, category)
//! rather than formatted strings — no trace blobs, no raw DETAIL/HINT from `PostgreSQL`.

use thiserror::Error;

/// Errors produced by the IR → SQL compilation stage.
///
/// All variants carry structured fields (model name, field name, operator, etc.)
/// rather than free-form strings — safe to surface in Ferrum error messages without
/// leaking user-supplied values or credentials.
#[derive(Debug, Error)]
pub enum CompileError {
    /// A `FieldRef` index in the IR does not correspond to any field in
    /// `ModelMetadata::fields`. Indicates a Python/Rust IR version skew or a
    /// bug in `QuerySet._build_ir()`; never reachable from valid user input alone.
    #[error("unknown field '{field}' on model '{model}'")]
    UnknownField { model: String, field: String },

    /// The `Filter::operator` string is not in `FieldMeta::allowed_operators`.
    /// Fail-fast before any SQL text is produced (SQL-1).
    #[error("unsupported operator '{operator}' for field '{field}' on model '{model}'")]
    UnsupportedOperator {
        model: String,
        field: String,
        operator: String,
    },

    /// `SortDirection::Unknown` was present in `OrderBy::direction`.
    /// Produced when serde deserializes an unrecognized direction string from
    /// Python/JSON (Defense in Depth — compile rejects before SQL emission).
    #[error("invalid sort direction '{direction}' for field '{field}' on model '{model}'")]
    InvalidSortDirection {
        model: String,
        field: String,
        direction: String,
    },

    /// `QuerySetIR::version` does not equal `IR_VERSION`. Indicates the Python
    /// side was built against a different Ferrum Rust version.
    #[error("IR version {got} is not supported (expected {expected})")]
    IrVersionMismatch { expected: u32, got: u32 },

    /// The IR is structurally invalid in a way not covered by the other variants
    /// (e.g. empty bulk rows, mismatched column counts, wrong operation kind
    /// passed to an emitter). Not reachable from well-formed Python-side IR.
    #[error("malformed IR: {reason}")]
    MalformedIr { reason: String },

    /// Unscoped mutation guard: UPDATE and DELETE must have at least one filter.
    /// Callers must use the danger API (`danger_update_all` / `danger_delete_all`)
    /// to bypass this (see AGENTS.md §3 and §5 MIG-5).
    #[error("operation '{operation}' on model '{model}' requires at least one filter; use the danger API for unscoped mutations")]
    MissingFilter { model: String, operation: String },
}

/// Errors produced by the row-hydration stage.
///
/// All variants carry structured fields only — no raw `DETAIL`/`HINT` strings
/// from the database driver or row data is included (observability Tier A contract).
#[derive(Debug, Error)]
pub enum HydrateError {
    /// A non-nullable column is absent from the result row (column not projected).
    ///
    /// Root cause: the SELECT projection omitted a required column, or the query
    /// used `only()`/`defer()` and the deferred field was accessed unexpectedly.
    #[error("column '{column}' missing from result set for model '{model}'")]
    MissingColumn { model: String, column: String },

    /// A non-nullable column is present in the row but carries a NULL value.
    ///
    /// Indicates a schema/DB constraint violation (the column was defined `NOT NULL`
    /// in Ferrum metadata but the DB returned NULL) rather than a missing projection.
    /// Kept as a distinct variant so callers can triage the root cause.
    #[error("non-nullable column '{column}' on model '{model}' contains NULL")]
    NullNonNullable { model: String, column: String },

    /// The JSON type of a column's value does not match the declared `FieldType`.
    ///
    /// In practice this is rare because Pydantic handles coercion on the Python
    /// side (ADR-003 fast path). Reserved for future strict-mode hydration.
    #[error(
        "type mismatch for column '{column}' on model '{model}': expected {expected}, got {got}"
    )]
    TypeMismatch {
        model: String,
        column: String,
        expected: String,
        got: String,
    },
}

/// Errors produced by the migration-plan stage.
///
/// The plan stage is pure and stateless — errors here indicate a schema diff
/// that cannot be resolved unambiguously or a ledger integrity check failure.
#[derive(Debug, Error)]
pub enum PlanError {
    /// The schema diff between `current_schema` and `target_schema` is
    /// ambiguous for the given table (e.g. a column rename vs drop+add).
    /// The Python side should surface `reason` to the developer for manual resolution.
    #[error("schema diff produced an ambiguous migration for table '{table}': {reason}")]
    AmbiguousDiff { table: String, reason: String },

    /// The SHA-256 digest stored in the migration ledger does not match the
    /// digest recomputed from the current plan. Indicates an out-of-band edit
    /// to a migration file or ledger corruption.
    #[error("migration plan digest mismatch: stored={stored}, computed={computed}")]
    DigestMismatch { stored: String, computed: String },
}
