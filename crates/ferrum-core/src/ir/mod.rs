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
pub const IR_VERSION: u32 = 4;

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

    /// Optional full-text relevance ordering (``QuerySet.rank_by`` / ``QuerySet.search``).
    #[serde(default)]
    pub text_rank_by: Option<TextRankBy>,

    /// Optional boolean predicate tree (``Q`` objects). When present, combined with
    /// ``filters`` (AND). When absent, ``filters`` are AND-ed as in IR v1.
    #[serde(default)]
    pub predicate: Option<Predicate>,

    /// Emit ``SELECT DISTINCT`` (`PostgreSQL`).
    #[serde(default)]
    pub distinct: bool,

    /// Compile to ``SELECT EXISTS(subquery)`` — used by ``QuerySet.exists()``.
    #[serde(default)]
    pub exists: bool,

    /// To-one JOINs for ``select_related()`` (validated on the Python side).
    #[serde(default)]
    pub joins: Vec<JoinSpec>,

    /// Optional typed aggregate projection.
    #[serde(default)]
    pub aggregation: Option<Aggregation>,
}

/// Aggregate projection for grouped and scalar aggregate queries.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Aggregation {
    /// GROUP BY expressions, emitted in this order.
    pub groups: Vec<GroupExpression>,
    /// Aggregate expressions, emitted after grouping keys.
    pub aggregates: Vec<AggregateExpression>,
    /// HAVING constraints referencing aggregate positions.
    #[serde(default)]
    pub having: Vec<Having>,
}

/// A metadata-backed GROUP BY expression.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum GroupExpression {
    /// Group directly by a model field.
    Field { field: FieldRef },
    /// Group a date/datetime field by a fixed truncation bucket.
    DateTrunc {
        field: FieldRef,
        granularity: DateTruncGranularity,
    },
}

/// Fixed `DATE_TRUNC` buckets. Unknown input is rejected during deserialization.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum DateTruncGranularity {
    Minute,
    Hour,
    Day,
    Week,
    Month,
    Quarter,
    Year,
}

/// One typed aggregate projection.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AggregateExpression {
    pub function: AggregateFunction,
    /// COUNT may omit a field to emit COUNT(*); all other functions require one.
    pub field: Option<FieldRef>,
    /// `PostgreSQL` FILTER predicate, validated through the normal predicate path.
    #[serde(default)]
    pub filter: Option<Predicate>,
}

/// Aggregate functions supported by Ferrum.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum AggregateFunction {
    Count,
    Sum,
    Avg,
    Min,
    Max,
}

/// A HAVING constraint against an aggregate by zero-based projection index.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Having {
    pub aggregate_index: usize,
    pub operator: HavingOperator,
    pub value: BindValue,
}

/// Fixed HAVING comparison operators.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum HavingOperator {
    Eq,
    Ne,
    Gt,
    Gte,
    Lt,
    Lte,
}

/// JOIN kind for ``select_related`` (LEFT) vs relation-filter (INNER) joins.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, Default)]
#[serde(rename_all = "snake_case")]
pub enum JoinKind {
    /// ``LEFT JOIN`` — used by ``select_related()`` (preserve parent rows).
    #[default]
    Left,
    /// ``INNER JOIN`` — used by relation-filter lookups (``team__slug=...``).
    Inner,
}

/// JOIN metadata for ``select_related`` and relation-filter lookups.
///
/// The emitter produces `<JOIN KIND> <remote_table> AS <alias> ON <base_table>.<local_col> = <alias>.<remote_pk_column>`.
/// All identifier fields are validated on the Python side before this struct is serialized into the IR.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct JoinSpec {
    /// Python relation name (e.g. `"author"`) — used in aliased column output keys.
    pub relation: String,
    /// SQL alias for the joined table (e.g. `"author"`).
    pub alias: String,
    /// The FK field on the base model that joins to the remote PK.
    pub local_field: FieldRef,
    /// Remote table name (from model metadata, not user input).
    pub remote_table: String,
    /// Remote table PK column name (from model metadata).
    pub remote_pk_column: String,
    /// Columns available on the joined table (for SELECT projection and/or filter allowlisting).
    pub remote_fields: Vec<JoinFieldRef>,
    /// ``left`` (default) or ``inner``.
    #[serde(default)]
    pub join_kind: JoinKind,
    /// When ``true`` (default), project ``remote_fields`` into ``alias__col`` SELECT aliases.
    /// Filter-only joins set this ``false`` so hydration is not polluted.
    #[serde(default = "default_true")]
    pub project_remote: bool,
}

fn default_true() -> bool {
    true
}

/// A field reference into a joined (remote) model's metadata.
///
/// When ``JoinSpec::project_remote`` is true, projected as
/// `<alias>.<column> AS "<alias>__<column>"` in the SELECT list.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct JoinFieldRef {
    /// Index into the remote model's `ModelMetadata::fields`.
    pub index: usize,
    /// Python attribute name of the remote field (used in output key construction).
    pub name: String,
    /// Database column name of the remote field.
    pub column: String,
    /// Operator allowlist copied from the remote field's ``FieldMeta`` (SQL-2).
    #[serde(default)]
    pub allowed_operators: Vec<String>,
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
        /// When ``true``, emit ``RETURNING *`` (`PostgreSQL`).
        #[serde(default = "default_true")]
        returning: bool,
    },
    /// PK-keyed multi-row UPDATE (``QuerySet.bulk_update``).
    /// ``pk_fields`` carries one entry per PK column (composite PK support).
    BulkUpdate {
        /// All PK fields in definition order (one for single-PK, many for composite).
        pk_fields: Vec<FieldRef>,
        fields: Vec<FieldRef>,
        rows: Vec<BulkUpdateRow>,
    },
    /// PK-keyed multi-row DELETE (``QuerySet.bulk_delete``).
    /// Each element of ``ids`` is a parallel vec of values, one per PK field.
    BulkDelete {
        /// All PK fields in definition order.
        pk_fields: Vec<FieldRef>,
        /// Each inner vec is one row's PK values in ``pk_fields`` order.
        ids: Vec<Vec<BindValue>>,
    },
}

/// One row payload for :variant:`Operation::BulkUpdate`.
/// ``pk_values`` has one entry per PK field (single entry for single-PK models).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BulkUpdateRow {
    /// PK column values in the same order as ``BulkUpdate::pk_fields``.
    pub pk_values: Vec<BindValue>,
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

/// A filter predicate (one leaf of the WHERE clause).
///
/// `operator` must be in `FieldMeta::allowed_operators` for `field` (base model)
/// or in the matching ``JoinFieldRef::allowed_operators`` when ``join_alias`` is
/// set. The compiler rejects any string not in that allowlist before SQL is
/// produced (SQL-1 / SQL-2).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Filter {
    /// The model field being filtered.
    ///
    /// When ``join_alias`` is set, ``index``/``name`` refer to the **remote**
    /// model's field (matched against ``JoinSpec::remote_fields``).
    pub field: FieldRef,
    /// Ferrum operator string, e.g. `"eq"`, `"gt"`, `"icontains"`, `"is_null"`.
    /// Validated against the field's operator allowlist before SQL emission.
    pub operator: String,
    /// Bound value for the filter — never interpolated into SQL text.
    pub value: BindValue,
    /// When set, the filter targets a column on the JOIN with this alias
    /// (Django-style ``relation__field`` lookups). ``None`` = base table.
    #[serde(default)]
    pub join_alias: Option<String>,
}

/// Composable boolean predicate tree (``Q`` objects on the Python side).
///
/// `And`/`Or`/`Not` compose leaf `Filter` nodes into arbitrarily nested WHERE
/// expressions. The compiler validates every leaf `Filter` against the metadata
/// allowlist before any SQL is produced.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum Predicate {
    /// All children must be true (SQL `… AND …`).
    And { children: Vec<Predicate> },
    /// At least one child must be true (SQL `… OR …`).
    Or { children: Vec<Predicate> },
    /// Negates the child predicate (SQL `NOT (…)`).
    Not { child: Box<Predicate> },
    /// A leaf filter — validated against `FieldMeta::allowed_operators`.
    Filter { filter: Filter },
}

/// An ORDER BY clause element.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct OrderBy {
    /// The model field to sort by (validated against the metadata allowlist).
    pub field: FieldRef,
    /// Sort direction; `Unknown` is rejected by the compiler before SQL is produced.
    pub direction: SortDirection,
}

/// pgvector distance metric for KNN ordering.
///
/// Each variant maps to a pgvector operator in the emitted SQL:
/// `L2` → `<->`, `Cosine` → `<=>`, `InnerProduct` → `<#>`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum VectorMetric {
    /// Euclidean (L2) distance — `ORDER BY col <-> $n`.
    L2,
    /// Cosine distance — `ORDER BY col <=> $n`.
    Cosine,
    /// Negative inner product — `ORDER BY col <#> $n`.
    InnerProduct,
}

/// KNN vector ordering (``ORDER BY col <-> $n``).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct VectorOrderBy {
    pub field: FieldRef,
    pub metric: VectorMetric,
    pub value: BindValue,
}

/// Full-text query parsing mode for ``match_*`` filters and ``text_rank_by``.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum TextSearchMode {
    /// Natural-language / plain terms (``plainto_tsquery``, ``FREETEXT``, etc.).
    Plain,
    /// Exact phrase (``phraseto_tsquery``, phrase mode).
    Phrase,
    /// Web-style search syntax (``websearch_to_tsquery``).
    Websearch,
    /// Boolean / prefix query DSL (``to_tsquery``, ``IN BOOLEAN MODE``).
    Boolean,
}

/// Full-text relevance ordering (``ORDER BY <rank> DESC``).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TextRankBy {
    pub field: FieldRef,
    pub query: BindValue,
    pub mode: TextSearchMode,
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
    /// `PostgreSQL` text[] array parameter.
    TextArray(Vec<String>),
    /// `PostgreSQL` integer[] array parameter.
    IntArray(Vec<i64>),
}
