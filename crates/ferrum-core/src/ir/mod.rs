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
pub const IR_VERSION: u32 = 2;

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

    /// Optional pgvector KNN ordering (``QuerySet.nearest_to``).
    #[serde(default)]
    pub vector_order_by: Option<VectorOrderBy>,

    /// Optional boolean predicate tree (``Q`` objects). When present, combined with
    /// ``filters`` (AND). When absent, ``filters`` are AND-ed as in IR v1.
    #[serde(default)]
    pub predicate: Option<Predicate>,

    /// Emit ``SELECT DISTINCT`` (PostgreSQL).
    #[serde(default)]
    pub distinct: bool,

    /// Compile to ``SELECT EXISTS(subquery)`` — used by ``QuerySet.exists()``.
    #[serde(default)]
    pub exists: bool,

    /// To-one JOINs for ``select_related()`` (validated on the Python side).
    #[serde(default)]
    pub joins: Vec<JoinSpec>,
}

/// JOIN metadata for ``select_related`` (PostgreSQL LEFT JOIN).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct JoinSpec {
    pub relation: String,
    pub alias: String,
    pub local_field: FieldRef,
    pub remote_table: String,
    pub remote_pk_column: String,
    pub remote_fields: Vec<JoinFieldRef>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct JoinFieldRef {
    pub index: usize,
    pub name: String,
    pub column: String,
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
        /// When `true`, the Rust compiler skips the `MissingFilter` guard.
        /// Set only by `QuerySet.danger_update_all()` — never by `.update()`.
        #[serde(default)]
        danger: bool,
    },
    Delete {
        /// When `true`, the Rust compiler skips the `MissingFilter` guard.
        /// Set only by `QuerySet.danger_delete_all()` — never by `.delete()`.
        #[serde(default)]
        danger: bool,
    },
    /// Multi-row INSERT (``QuerySet.bulk_create``).
    BulkInsert {
        /// Each inner vec is one row: ``(FieldRef, BindValue)`` pairs in column order.
        rows: Vec<Vec<(FieldRef, BindValue)>>,
        /// When ``true``, emit ``RETURNING *`` (PostgreSQL).
        #[serde(default = "default_true")]
        returning: bool,
    },
    /// PK-keyed multi-row UPDATE (``QuerySet.bulk_update``).
    BulkUpdate {
        pk_field: FieldRef,
        fields: Vec<FieldRef>,
        rows: Vec<BulkUpdateRow>,
    },
    /// PK-keyed multi-row DELETE (``QuerySet.bulk_delete``).
    BulkDelete {
        pk_field: FieldRef,
        ids: Vec<BindValue>,
    },
}

fn default_true() -> bool {
    true
}

/// One row payload for :variant:`Operation::BulkUpdate`.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BulkUpdateRow {
    pub pk: BindValue,
    pub values: Vec<BindValue>,
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

/// Composable boolean predicate tree (``Q`` objects on the Python side).
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum Predicate {
    And { children: Vec<Predicate> },
    Or { children: Vec<Predicate> },
    Not { child: Box<Predicate> },
    Filter { filter: Filter },
}

/// An ORDER BY clause element.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct OrderBy {
    pub field: FieldRef,
    pub direction: SortDirection,
}

/// pgvector distance metric for KNN ordering.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum VectorMetric {
    L2,
    Cosine,
    InnerProduct,
}

/// KNN vector ordering (``ORDER BY col <-> $n``).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct VectorOrderBy {
    pub field: FieldRef,
    pub metric: VectorMetric,
    pub value: BindValue,
}

/// Sort direction — only `Asc` and `Desc` are valid; anything else deserialized
/// from Python/JSON becomes `Unknown`, which the compiler rejects with
/// `CompileError::InvalidSortDirection` before any SQL is produced.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum SortDirection {
    Asc,
    Desc,
    /// Catch-all for unrecognized direction strings arriving from Python/JSON.
    /// Valid Rust code never constructs this; it is produced only by serde
    /// when the JSON value is not `"asc"` or `"desc"`. The compiler rejects it
    /// before emitting any SQL text (Defense in Depth, SQL-1).
    #[serde(other)]
    Unknown,
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
    /// pgvector query vector — list of f64 components.
    FloatArray(Vec<f64>),
}
