//! Model metadata — the allowlists that drive safe SQL compilation.
//!
//! `ModelMetadata` is built once at Python class-definition time and is immutable
//! thereafter. Compilation is a pure function `(&ModelMetadata, QuerySetIR) → ...`.
//! No per-request mutable shared state lives here.

use serde::{Deserialize, Serialize};

/// Complete, immutable description of a Ferrum model as seen by the Rust compiler.
///
/// Field/operator/sort allowlists live here so compilation never has to trust
/// runtime user input for identifier construction.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ModelMetadata {
    /// Python class name; used in error messages only.
    pub model_name: String,

    /// Database table name (validated at model-definition time, not user input).
    pub table_name: String,

    /// Ordered list of field descriptors. `FieldRef::index` addresses this list.
    pub fields: Vec<FieldMeta>,

    /// Primary key field index (into `fields`). Legacy single-PK alias.
    /// For composite PKs use `pk_fields`; `pk_index` is `pk_fields[0]`.
    pub pk_index: usize,

    /// All primary-key field indices (into `fields`) in definition order.
    /// Empty vec is treated as `[pk_index]` for backward compatibility.
    #[serde(default)]
    pub pk_fields: Vec<usize>,
}

/// Metadata for a single model field.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct FieldMeta {
    /// Python attribute name.
    pub name: String,

    /// Database column name.
    pub column_name: String,

    /// Ferrum type tag used by the hydrator to decode raw row bytes.
    pub field_type: FieldType,

    /// Operators permitted for this field in WHERE clauses.
    /// Compilation fails with `CompileError::UnsupportedOperator` for anything outside this set.
    pub allowed_operators: Vec<String>,

    /// Whether this column may be NULL.
    pub nullable: bool,

    /// pgvector dimensionality (DDL-only metadata; optional).
    #[serde(default)]
    pub vector_dimensions: Option<u32>,
}

/// Ferrum-level type tag for a field. Corresponds to Pydantic / Python types.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum FieldType {
    Int,
    BigInt,
    Float,
    Decimal,
    Text,
    Bool,
    Datetime,
    Date,
    Time,
    Uuid,
    Json,
    Bytes,
    Vector,
    #[serde(alias = "tsvector")]
    TsVector,
    /// `PostgreSQL` TEXT[] column.
    ArrayText,
    /// `PostgreSQL` INTEGER[] column.
    ArrayInt,
    /// `PostgreSQL` UUID[] column (elements serialized as text strings).
    ArrayUuid,
    /// `PostgreSQL` FLOAT8[] column.
    ArrayFloat,
    /// Enum stored as TEXT with a CHECK constraint.
    Enum,
}

impl ModelMetadata {
    /// A stable Tier A observability key: `"<operation>:<model_name>"`.
    ///
    /// Used in `query_start` / `query_success` hook events before or after
    /// compilation. Contains no bound values, no user input, and no credentials.
    #[must_use]
    pub fn query_fingerprint(&self, operation: &str) -> String {
        format!("{}:{}", operation, self.model_name)
    }

    /// Returns the effective PK field indices.
    ///
    /// Uses `pk_fields` if non-empty; falls back to `[pk_index]` for backward
    /// compatibility with metadata serialized without `pk_fields`.
    #[must_use]
    pub fn effective_pk_fields(&self) -> Vec<usize> {
        if self.pk_fields.is_empty() {
            vec![self.pk_index]
        } else {
            self.pk_fields.clone()
        }
    }
}
