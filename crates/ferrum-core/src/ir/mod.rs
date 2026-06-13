//! `QuerySet` intermediate representation (IR).
//!
//! `QuerySetIR` is the typed, versioned, serializable contract that crosses the `PyO3`
//! boundary from Python into Rust. The version field allows the Rust side to reject
//! IR produced by an incompatible Python version before any compilation occurs.
//!
//! Design constraints (ADR-002 in progress):
//! - Values are carried out-of-band from identifiers — field names are indices into the
//!   model metadata allowlist, never raw user strings in an identifier position.
//! - Bound parameter values are `BindValue` variants, never interpolated into SQL.

use serde::{Deserialize, Serialize};

pub mod metadata;
pub use metadata::ModelMetadata;

/// Version of the IR contract this crate implements.
pub const IR_VERSION: u32 = 1;

/// The root IR node produced by `QuerySet._build_ir()` on the Python side.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct QuerySetIR {
    /// Must equal `IR_VERSION`; rejected before compilation if mismatched.
    pub version: u32,

    /// Model identifier (maps to a registered `ModelMetadata` entry).
    pub model_name: String,

    /// Operation to compile.
    pub operation: Operation,

    /// WHERE clause filters.
    pub filters: Vec<Filter>,

    /// ORDER BY clauses.
    pub order_by: Vec<OrderBy>,

    /// LIMIT row count, if any.
    pub limit: Option<u64>,

    /// OFFSET row count, if any.
    pub offset: Option<u64>,
}

/// The SQL operation this IR node represents.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum Operation {
    Select {
        fields: Vec<FieldRef>,
    },
    Insert {
        values: Vec<(FieldRef, BindValue)>,
    },
    Update {
        assignments: Vec<(FieldRef, BindValue)>,
    },
    Delete,
}

/// A reference to a field, validated against the model metadata allowlist.
/// The `index` is a pre-validated index into `ModelMetadata::fields`; the
/// `name` is preserved for error messages only.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct FieldRef {
    pub name: String,
    pub index: usize,
}

/// A filter predicate.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Filter {
    pub field: FieldRef,
    pub operator: String,
    pub value: BindValue,
}

/// An ORDER BY clause element.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct OrderBy {
    pub field: FieldRef,
    pub direction: SortDirection,
}

/// Sort direction — only these two values are allowed; anything else is a
/// `CompileError::InvalidSortDirection`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum SortDirection {
    Asc,
    Desc,
}

/// A bound parameter value carried out-of-band from SQL identifiers.
///
/// The Python side serializes Python types to one of these variants;
/// the Rust side encodes them as `$N` placeholders in SQL text and
/// produces a parallel `bound_params` list.
///
/// User-supplied strings are *only* `BindValue::Text`; they never
/// reach the identifier positions of the SQL AST.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "type", content = "value", rename_all = "snake_case")]
pub enum BindValue {
    Null,
    Bool(bool),
    Int(i64),
    Float(f64),
    Text(String),
    Bytes(Vec<u8>),
    /// ISO-8601 datetime string — decoded by the DB driver on the Python side.
    Datetime(String),
}
