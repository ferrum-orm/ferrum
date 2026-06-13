//! Structured error types for the Ferrum core engine.
//!
//! All errors carry structured fields (model name, field name, operator, category)
//! rather than formatted strings — no trace blobs, no raw DETAIL/HINT from `PostgreSQL`.

use thiserror::Error;

/// Errors produced by the IR → SQL compilation stage.
#[derive(Debug, Error)]
pub enum CompileError {
    #[error("unknown field '{field}' on model '{model}'")]
    UnknownField { model: String, field: String },

    #[error("unsupported operator '{operator}' for field '{field}' on model '{model}'")]
    UnsupportedOperator {
        model: String,
        field: String,
        operator: String,
    },

    #[error("invalid sort direction '{direction}' for field '{field}' on model '{model}'")]
    InvalidSortDirection {
        model: String,
        field: String,
        direction: String,
    },

    #[error("IR version {got} is not supported (expected {expected})")]
    IrVersionMismatch { expected: u32, got: u32 },

    #[error("malformed IR: {reason}")]
    MalformedIr { reason: String },
}

/// Errors produced by the row-hydration stage.
#[derive(Debug, Error)]
pub enum HydrateError {
    #[error("column '{column}' missing from result set for model '{model}'")]
    MissingColumn { model: String, column: String },

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
#[derive(Debug, Error)]
pub enum PlanError {
    #[error("schema diff produced an ambiguous migration for table '{table}': {reason}")]
    AmbiguousDiff { table: String, reason: String },

    #[error("migration plan digest mismatch: stored={stored}, computed={computed}")]
    DigestMismatch { stored: String, computed: String },
}
