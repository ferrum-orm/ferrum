//! SQL emitters for `PostgreSQL`: SELECT (Wave 1). INSERT/UPDATE/DELETE follow in Wave 3.
//!
//! Each emitter takes validated IR nodes (field refs already checked against
//! allowlists in `ferrum-core::compile`) and produces parameterized SQL text.
//!
//! # Security invariants (SQL-1 / SQL-2)
//! - Identifiers (table names, column names) come exclusively from
//!   `ModelMetadata` allowlists, never from user input.
//! - Every value — including LIMIT and OFFSET — travels as a `$N` bound
//!   parameter, never interpolated into the SQL text.
//! - `bound_params` never contains SQL identifier strings.

use crate::dialect;
use ferrum_core::{
    compile::CompiledQuery,
    error::CompileError,
    ir::{BindValue, ModelMetadata, QuerySetIR, SortDirection, VectorMetric},
};
use std::fmt::Write as _;

/// Emit a full parameterized SELECT statement from a `QuerySetIR`.
///
/// Validation is delegated to `ferrum_core::compile::compile` first; the
/// function returns early with any validation error before touching SQL.
///
/// # Errors
/// Propagates `CompileError` from allowlist validation or malformed IR.
pub fn emit_select(
    metadata: &ModelMetadata,
    ir: &QuerySetIR,
) -> Result<CompiledQuery, CompileError> {
    // Run all allowlist validation first — fail before any SQL exists (SQL-1).
    ferrum_core::compile::compile(metadata, ir)?;

    // Build SELECT clause from validated field refs.
    let table = dialect::quote_ident(&metadata.table_name);
    let fields = match &ir.operation {
        ferrum_core::ir::Operation::Select { fields } => fields
            .iter()
            .map(|f| {
                let col = &metadata.fields[f.index].column_name;
                dialect::quote_ident(col)
            })
            .collect::<Vec<_>>()
            .join(", "),
        _ => {
            return Err(CompileError::MalformedIr {
                reason: "emit_select called with non-Select operation".into(),
            })
        }
    };

    let mut bound_params: Vec<BindValue> = Vec::new();
    let mut param_type_summary: Vec<String> = Vec::new();
    let mut where_clauses: Vec<String> = Vec::new();

    for filter in &ir.filters {
        let col = dialect::quote_ident(&metadata.fields[filter.field.index].column_name);
        let (clause, param) = filter_clause(
            &col,
            &filter.operator,
            bound_params.len() + 1,
            filter.value.clone(),
        );
        where_clauses.push(clause);
        if let Some(value) = param {
            param_type_summary.push(format!("{}:{}", filter.field.name, filter.operator));
            bound_params.push(value);
        }
    }

    let mut sql = format!("SELECT {fields} FROM {table}");
    if !where_clauses.is_empty() {
        sql.push_str(" WHERE ");
        sql.push_str(&where_clauses.join(" AND "));
    }

    if !ir.order_by.is_empty() {
        let mut order_parts: Vec<String> = Vec::new();
        for o in &ir.order_by {
            let col = dialect::quote_ident(&metadata.fields[o.field.index].column_name);
            // `Unknown` is already rejected by ferrum_core::compile above;
            // this arm is here for exhaustiveness (Defense in Depth).
            let dir = match o.direction {
                SortDirection::Asc => "ASC",
                SortDirection::Desc => "DESC",
                SortDirection::Unknown => {
                    return Err(CompileError::InvalidSortDirection {
                        model: metadata.model_name.clone(),
                        field: o.field.name.clone(),
                        direction: "unknown".into(),
                    })
                }
            };
            order_parts.push(format!("{col} {dir}"));
        }
        sql.push_str(" ORDER BY ");
        sql.push_str(&order_parts.join(", "));
    } else if let Some(vector_order) = &ir.vector_order_by {
        let col = dialect::quote_ident(&metadata.fields[vector_order.field.index].column_name);
        let op = vector_metric_to_sql(vector_order.metric);
        let placeholder = dialect::placeholder(bound_params.len() + 1);
        write!(sql, " ORDER BY {col} {op} {placeholder}").expect("write to String is infallible");
        param_type_summary.push(format!("{}:nearest_to", vector_order.field.name));
        bound_params.push(vector_order.value.clone());
    }

    // LIMIT and OFFSET are bound parameters — values are never interpolated
    // into SQL text, even for integer-only inputs (SQL-2, Tier A discipline).
    if let Some(limit) = ir.limit {
        let placeholder = dialect::placeholder(bound_params.len() + 1);
        write!(sql, " LIMIT {placeholder}").expect("write to String is infallible");
        param_type_summary.push("limit:int".into());
        bound_params.push(BindValue::Int(i64::try_from(limit).unwrap_or(i64::MAX)));
    }
    if let Some(offset) = ir.offset {
        let placeholder = dialect::placeholder(bound_params.len() + 1);
        write!(sql, " OFFSET {placeholder}").expect("write to String is infallible");
        param_type_summary.push("offset:int".into());
        bound_params.push(BindValue::Int(i64::try_from(offset).unwrap_or(i64::MAX)));
    }

    let fingerprint = sql_fingerprint(&sql);

    Ok(CompiledQuery {
        sql_text: sql,
        bound_params,
        param_type_summary,
        fingerprint,
    })
}

/// Emit a full parameterized INSERT … RETURNING statement from a `QuerySetIR`.
///
/// The RETURNING clause projects all model fields so the caller can hydrate the
/// inserted row without a second SELECT.
///
/// # Errors
/// Propagates `CompileError` from allowlist validation or malformed IR.
pub fn emit_insert(
    metadata: &ModelMetadata,
    ir: &QuerySetIR,
) -> Result<CompiledQuery, CompileError> {
    ferrum_core::compile::compile(metadata, ir)?;

    let ferrum_core::ir::Operation::Insert { values } = &ir.operation else {
        return Err(CompileError::MalformedIr {
            reason: "emit_insert called with non-Insert operation".into(),
        });
    };

    let table = dialect::quote_ident(&metadata.table_name);
    let mut bound_params: Vec<BindValue> = Vec::new();
    let mut param_type_summary: Vec<String> = Vec::new();
    let mut col_names: Vec<String> = Vec::new();
    let mut placeholders: Vec<String> = Vec::new();

    for (field_ref, value) in values {
        // Column name from the metadata allowlist — never the raw user string.
        col_names.push(dialect::quote_ident(
            &metadata.fields[field_ref.index].column_name,
        ));
        let ph = dialect::placeholder(bound_params.len() + 1);
        placeholders.push(ph);
        param_type_summary.push(format!("{}:insert", field_ref.name));
        bound_params.push(value.clone());
    }

    // RETURNING all model fields so the Python side can hydrate the inserted row.
    let returning = returning_all_fields(metadata);

    let sql = format!(
        "INSERT INTO {table} ({cols}) VALUES ({vals}) RETURNING {returning}",
        cols = col_names.join(", "),
        vals = placeholders.join(", "),
    );
    let fingerprint = sql_fingerprint(&sql);

    Ok(CompiledQuery {
        sql_text: sql,
        bound_params,
        param_type_summary,
        fingerprint,
    })
}

/// Emit a full parameterized UPDATE … RETURNING statement from a `QuerySetIR`.
///
/// Requires at least one filter (`CompileError::MissingFilter` propagated from
/// `compile()` when `ir.filters` is empty — the unscoped-mutation guard).
///
/// # Errors
/// Propagates `CompileError` from allowlist validation or malformed IR.
pub fn emit_update(
    metadata: &ModelMetadata,
    ir: &QuerySetIR,
) -> Result<CompiledQuery, CompileError> {
    // `compile` enforces MissingFilter for unfiltered UPDATE.
    ferrum_core::compile::compile(metadata, ir)?;

    let ferrum_core::ir::Operation::Update { assignments, .. } = &ir.operation else {
        return Err(CompileError::MalformedIr {
            reason: "emit_update called with non-Update operation".into(),
        });
    };

    let table = dialect::quote_ident(&metadata.table_name);
    let mut bound_params: Vec<BindValue> = Vec::new();
    let mut param_type_summary: Vec<String> = Vec::new();
    let mut set_clauses: Vec<String> = Vec::new();

    for (field_ref, value) in assignments {
        let col = dialect::quote_ident(&metadata.fields[field_ref.index].column_name);
        let ph = dialect::placeholder(bound_params.len() + 1);
        set_clauses.push(format!("{col} = {ph}"));
        param_type_summary.push(format!("{}:assign", field_ref.name));
        bound_params.push(value.clone());
    }

    let mut where_clauses: Vec<String> = Vec::new();
    for filter in &ir.filters {
        let col = dialect::quote_ident(&metadata.fields[filter.field.index].column_name);
        let (clause, param) = filter_clause(
            &col,
            &filter.operator,
            bound_params.len() + 1,
            filter.value.clone(),
        );
        where_clauses.push(clause);
        if let Some(value) = param {
            param_type_summary.push(format!("{}:{}", filter.field.name, filter.operator));
            bound_params.push(value);
        }
    }

    let returning = returning_all_fields(metadata);

    let mut sql = format!("UPDATE {table} SET {}", set_clauses.join(", "));
    write!(sql, " WHERE {}", where_clauses.join(" AND ")).expect("write to String is infallible");
    write!(sql, " RETURNING {returning}").expect("write to String is infallible");

    let fingerprint = sql_fingerprint(&sql);

    Ok(CompiledQuery {
        sql_text: sql,
        bound_params,
        param_type_summary,
        fingerprint,
    })
}

/// Emit a full parameterized DELETE statement from a `QuerySetIR`.
///
/// Requires at least one filter (`CompileError::MissingFilter` propagated from
/// `compile()` when `ir.filters` is empty — the unscoped-mutation guard).
///
/// # Errors
/// Propagates `CompileError` from allowlist validation or malformed IR.
pub fn emit_delete(
    metadata: &ModelMetadata,
    ir: &QuerySetIR,
) -> Result<CompiledQuery, CompileError> {
    // `compile` enforces MissingFilter for unfiltered DELETE.
    ferrum_core::compile::compile(metadata, ir)?;

    let table = dialect::quote_ident(&metadata.table_name);
    let mut bound_params: Vec<BindValue> = Vec::new();
    let mut param_type_summary: Vec<String> = Vec::new();
    let mut where_clauses: Vec<String> = Vec::new();

    for filter in &ir.filters {
        let col = dialect::quote_ident(&metadata.fields[filter.field.index].column_name);
        let (clause, param) = filter_clause(
            &col,
            &filter.operator,
            bound_params.len() + 1,
            filter.value.clone(),
        );
        where_clauses.push(clause);
        if let Some(value) = param {
            param_type_summary.push(format!("{}:{}", filter.field.name, filter.operator));
            bound_params.push(value);
        }
    }

    let sql = format!("DELETE FROM {table} WHERE {}", where_clauses.join(" AND "));
    let fingerprint = sql_fingerprint(&sql);

    Ok(CompiledQuery {
        sql_text: sql,
        bound_params,
        param_type_summary,
        fingerprint,
    })
}

/// Build the `RETURNING` list for all model fields (used by INSERT and UPDATE).
///
/// Uses the metadata allowlist, never user input.
fn returning_all_fields(metadata: &ModelMetadata) -> String {
    metadata
        .fields
        .iter()
        .map(|f| dialect::quote_ident(&f.column_name))
        .collect::<Vec<_>>()
        .join(", ")
}

/// Build a WHERE predicate and optional bound parameter for a filter.
fn filter_clause(
    col: &str,
    operator: &str,
    param_index: usize,
    value: BindValue,
) -> (String, Option<BindValue>) {
    match operator {
        "is_null" => (format!("{col} IS NULL"), None),
        "is_not_null" => (format!("{col} IS NOT NULL"), None),
        "match" => {
            let placeholder = dialect::placeholder(param_index);
            (
                format!("{col} @@ plainto_tsquery({placeholder})"),
                Some(value),
            )
        }
        op => {
            let placeholder = dialect::placeholder(param_index);
            let sql_op = operator_to_sql(op);
            (format!("{col} {sql_op} {placeholder}"), Some(value))
        }
    }
}

fn vector_metric_to_sql(metric: VectorMetric) -> &'static str {
    match metric {
        VectorMetric::L2 => "<->",
        VectorMetric::Cosine => "<=>",
        VectorMetric::InnerProduct => "<#>",
    }
}

/// Map a Ferrum operator string to its `PostgreSQL` SQL fragment.
///
/// Only operators in the per-field allowlists can reach this function;
/// the `_` arm covers `"eq"` and is never hit for disallowed operators.
fn operator_to_sql(op: &str) -> &'static str {
    match op {
        "ne" => "<>",
        "gt" => ">",
        "gte" => ">=",
        "lt" => "<",
        "lte" => "<=",
        "is_null" => "IS NULL",
        "is_not_null" => "IS NOT NULL",
        "icontains" => "ILIKE",
        "contains" => "LIKE",
        _ => "=", // covers "eq"; unreachable for non-allowlisted operators
    }
}

/// Compute a stable FNV-1a fingerprint over the SQL shape (identifiers +
/// placeholder positions; no bound values appear in `sql_text`).
///
/// The fingerprint is used as the Tier A observability key and as a future
/// plan-cache key. It is deterministic and requires no external dependencies.
fn sql_fingerprint(sql: &str) -> String {
    const OFFSET_BASIS: u64 = 14_695_981_039_346_656_037;
    const PRIME: u64 = 1_099_511_628_211;
    let mut hash = OFFSET_BASIS;
    for byte in sql.bytes() {
        hash ^= u64::from(byte);
        hash = hash.wrapping_mul(PRIME);
    }
    format!("{hash:016x}")
}

#[cfg(test)]
mod tests {
    use super::*;
    use ferrum_core::ir::{
        metadata::{FieldMeta, FieldType},
        BindValue, FieldRef, Filter, Operation, OrderBy, QuerySetIR, SortDirection, IR_VERSION,
    };

    fn make_metadata() -> ModelMetadata {
        ModelMetadata {
            model_name: "User".into(),
            table_name: "users".into(),
            fields: vec![
                FieldMeta {
                    name: "id".into(),
                    column_name: "id".into(),
                    field_type: FieldType::Int,
                    allowed_operators: vec!["eq".into(), "gt".into()],
                    nullable: false,
                    vector_dimensions: None,
                },
                FieldMeta {
                    name: "email".into(),
                    column_name: "email".into(),
                    field_type: FieldType::Text,
                    allowed_operators: vec!["eq".into(), "icontains".into()],
                    nullable: false,
                    vector_dimensions: None,
                },
            ],
            pk_index: 0,
        }
    }

    fn select_ir(fields: Vec<FieldRef>) -> QuerySetIR {
        QuerySetIR {
            version: IR_VERSION,
            model_name: "User".into(),
            operation: Operation::Select { fields },
            filters: vec![],
            order_by: vec![],
            limit: None,
            offset: None,
            vector_order_by: None,
        }
    }

    // --- Happy path ---

    #[test]
    fn emit_basic_select() {
        let meta = make_metadata();
        let ir = select_ir(vec![
            FieldRef {
                name: "id".into(),
                index: 0,
            },
            FieldRef {
                name: "email".into(),
                index: 1,
            },
        ]);
        let q = emit_select(&meta, &ir).unwrap();
        assert!(q.sql_text.contains("\"users\""));
        assert!(q.sql_text.contains("\"id\""));
        assert!(q.bound_params.is_empty());
        // Fingerprint must be non-empty and stable.
        assert!(!q.fingerprint.is_empty());
        assert_eq!(q.fingerprint, sql_fingerprint(&q.sql_text));
    }

    /// Happy path: filter value lives in `bound_params` as $1, not in `sql_text`.
    #[test]
    fn emit_select_with_filter_uses_placeholder() {
        let meta = make_metadata();
        let mut ir = select_ir(vec![FieldRef {
            name: "id".into(),
            index: 0,
        }]);
        ir.filters.push(Filter {
            field: FieldRef {
                name: "email".into(),
                index: 1,
            },
            operator: "eq".into(),
            value: BindValue::Text("x@example.com".into()),
        });
        let q = emit_select(&meta, &ir).unwrap();
        // Bound value must NOT appear in SQL text (SQL-2).
        assert!(
            !q.sql_text.contains("x@example.com"),
            "value must not be in sql_text"
        );
        assert_eq!(q.bound_params.len(), 1);
        assert!(q.sql_text.contains("$1"));
    }

    /// LIMIT and OFFSET must be bound parameters, not SQL literals.
    #[test]
    fn emit_select_limit_offset_are_bound_params() {
        let meta = make_metadata();
        let mut ir = select_ir(vec![FieldRef {
            name: "id".into(),
            index: 0,
        }]);
        ir.order_by.push(OrderBy {
            field: FieldRef {
                name: "id".into(),
                index: 0,
            },
            direction: SortDirection::Desc,
        });
        ir.limit = Some(10);
        ir.offset = Some(5);

        let q = emit_select(&meta, &ir).unwrap();
        assert!(q.sql_text.contains("ORDER BY"), "ORDER BY present");
        assert!(q.sql_text.contains("DESC"), "DESC direction");
        // Limit and offset must appear as placeholders, not literals.
        assert!(q.sql_text.contains("LIMIT $1"), "LIMIT must use $1");
        assert!(q.sql_text.contains("OFFSET $2"), "OFFSET must use $2");
        // No raw integers in SQL text.
        assert!(
            !q.sql_text.contains("LIMIT 10"),
            "literal 10 must not appear"
        );
        assert!(
            !q.sql_text.contains("OFFSET 5"),
            "literal 5 must not appear"
        );
        // bound_params carries the limit and offset values.
        assert_eq!(q.bound_params.len(), 2);
        assert!(matches!(q.bound_params[0], BindValue::Int(10)));
        assert!(matches!(q.bound_params[1], BindValue::Int(5)));
    }

    // --- Rejection paths ---

    /// Unknown field index in SELECT projection → `CompileError::UnknownField`.
    #[test]
    fn rejects_unknown_select_field_index() {
        let meta = make_metadata();
        let ir = select_ir(vec![FieldRef {
            name: "ghost".into(),
            index: 99,
        }]);
        let err = emit_select(&meta, &ir).unwrap_err();
        assert!(matches!(err, CompileError::UnknownField { .. }));
    }

    /// Unsupported operator for a field → `CompileError::UnsupportedOperator`.
    #[test]
    fn rejects_unsupported_operator() {
        let meta = make_metadata();
        let mut ir = select_ir(vec![FieldRef {
            name: "id".into(),
            index: 0,
        }]);
        ir.filters.push(Filter {
            field: FieldRef {
                name: "id".into(),
                index: 0,
            },
            operator: "icontains".into(), // not allowed for Int field
            value: BindValue::Int(42),
        });
        let err = emit_select(&meta, &ir).unwrap_err();
        assert!(matches!(err, CompileError::UnsupportedOperator { .. }));
    }

    /// Invalid sort direction → `CompileError::InvalidSortDirection` (caught at
    /// the compile-validation stage, before SQL is produced).
    #[test]
    fn rejects_invalid_sort_direction() {
        let meta = make_metadata();
        let mut ir = select_ir(vec![FieldRef {
            name: "id".into(),
            index: 0,
        }]);
        ir.order_by.push(OrderBy {
            field: FieldRef {
                name: "id".into(),
                index: 0,
            },
            direction: SortDirection::Unknown,
        });
        let err = emit_select(&meta, &ir).unwrap_err();
        assert!(matches!(err, CompileError::InvalidSortDirection { .. }));
    }

    // --- Invariant: bound_params never contains SQL identifier strings ---

    /// Assert that table/column names do not appear as `BindValue::Text` inside
    /// `bound_params`. Identifiers are quoted and placed in `sql_text`; user
    /// values travel out-of-band in `bound_params` only (SQL-1 + SQL-2).
    #[test]
    fn bound_params_never_contain_sql_identifiers() {
        let meta = make_metadata();
        let mut ir = select_ir(vec![
            FieldRef {
                name: "id".into(),
                index: 0,
            },
            FieldRef {
                name: "email".into(),
                index: 1,
            },
        ]);
        ir.filters.push(Filter {
            field: FieldRef {
                name: "email".into(),
                index: 1,
            },
            operator: "eq".into(),
            value: BindValue::Text("user@example.com".into()),
        });
        ir.limit = Some(20);

        let q = emit_select(&meta, &ir).unwrap();

        // No bound_param may carry a SQL identifier string (table/column name).
        for param in &q.bound_params {
            if let BindValue::Text(s) = param {
                assert_ne!(s.as_str(), "users", "table name in bound_params");
                assert_ne!(s.as_str(), "id", "column name in bound_params");
                assert_ne!(s.as_str(), "email", "column name in bound_params");
                assert_ne!(s.as_str(), "\"users\"", "quoted table name in bound_params");
                assert_ne!(s.as_str(), "\"id\"", "quoted column name in bound_params");
                assert_ne!(
                    s.as_str(),
                    "\"email\"",
                    "quoted column name in bound_params"
                );
            }
            // Int/Bool/Float/etc. variants cannot hold SQL identifier strings
            // by construction; no further check needed.
        }

        // The user value IS in bound_params.
        let has_email = q
            .bound_params
            .iter()
            .any(|p| matches!(p, BindValue::Text(s) if s == "user@example.com"));
        assert!(has_email, "expected email value in bound_params");

        // Identifiers appear only in sql_text, quoted.
        assert!(q.sql_text.contains("\"users\""), "table in sql_text");
        assert!(q.sql_text.contains("\"email\""), "column in sql_text");
    }

    #[test]
    fn fingerprint_is_stable_for_same_shape() {
        let meta = make_metadata();
        let ir = select_ir(vec![FieldRef {
            name: "id".into(),
            index: 0,
        }]);
        let q1 = emit_select(&meta, &ir).unwrap();
        let q2 = emit_select(&meta, &ir).unwrap();
        assert_eq!(q1.fingerprint, q2.fingerprint);
    }

    // ── INSERT tests ─────────────────────────────────────────────────────────

    fn insert_ir(values: Vec<(FieldRef, BindValue)>) -> QuerySetIR {
        QuerySetIR {
            version: IR_VERSION,
            model_name: "User".into(),
            operation: Operation::Insert { values },
            filters: vec![],
            order_by: vec![],
            limit: None,
            offset: None,
            vector_order_by: None,
        }
    }

    /// The user-supplied value must appear in `bound_params` as a `$N` placeholder,
    /// never interpolated into `sql_text` (SQL-2 invariant).
    #[test]
    fn emit_insert_uses_placeholder_for_values() {
        let meta = make_metadata();
        let ir = insert_ir(vec![(
            FieldRef {
                name: "email".into(),
                index: 1,
            },
            BindValue::Text("secret@example.com".into()),
        )]);
        let q = emit_insert(&meta, &ir).unwrap();

        // Value must NOT be in sql_text.
        assert!(
            !q.sql_text.contains("secret@example.com"),
            "user value must not appear in sql_text"
        );
        // Placeholder must be present.
        assert!(
            q.sql_text.contains("$1"),
            "placeholder $1 must be in sql_text"
        );
        // Column name from allowlist, quoted.
        assert!(
            q.sql_text.contains("\"email\""),
            "quoted column must be in sql_text"
        );
        // Table name, quoted.
        assert!(
            q.sql_text.contains("\"users\""),
            "quoted table must be in sql_text"
        );
        // RETURNING clause.
        assert!(
            q.sql_text.contains("RETURNING"),
            "RETURNING clause required"
        );
        // Value in bound_params.
        assert_eq!(q.bound_params.len(), 1);
        assert!(matches!(&q.bound_params[0], BindValue::Text(s) if s == "secret@example.com"));
    }

    #[test]
    fn emit_insert_returning_contains_all_fields() {
        let meta = make_metadata();
        let ir = insert_ir(vec![(
            FieldRef {
                name: "id".into(),
                index: 0,
            },
            BindValue::Int(1),
        )]);
        let q = emit_insert(&meta, &ir).unwrap();
        // RETURNING must include both model fields.
        assert!(q.sql_text.contains("\"id\""));
        assert!(q.sql_text.contains("\"email\""));
    }

    // ── UPDATE tests ─────────────────────────────────────────────────────────

    fn update_ir(assignments: Vec<(FieldRef, BindValue)>, filters: Vec<Filter>) -> QuerySetIR {
        QuerySetIR {
            version: IR_VERSION,
            model_name: "User".into(),
            operation: Operation::Update {
                assignments,
                danger: false,
            },
            filters,
            order_by: vec![],
            limit: None,
            offset: None,
            vector_order_by: None,
        }
    }

    /// Unfiltered UPDATE must be rejected with `MissingFilter` — unscoped-mutation guard.
    #[test]
    fn emit_update_rejects_empty_filters() {
        let meta = make_metadata();
        let ir = update_ir(
            vec![(
                FieldRef {
                    name: "email".into(),
                    index: 1,
                },
                BindValue::Text("x@x.com".into()),
            )],
            vec![],
        );
        let err = emit_update(&meta, &ir).unwrap_err();
        assert!(
            matches!(err, CompileError::MissingFilter { .. }),
            "expected MissingFilter, got {err:?}"
        );
    }

    #[test]
    fn emit_update_with_filter_uses_placeholders() {
        let meta = make_metadata();
        let ir = update_ir(
            vec![(
                FieldRef {
                    name: "email".into(),
                    index: 1,
                },
                BindValue::Text("new@example.com".into()),
            )],
            vec![Filter {
                field: FieldRef {
                    name: "id".into(),
                    index: 0,
                },
                operator: "eq".into(),
                value: BindValue::Int(42),
            }],
        );
        let q = emit_update(&meta, &ir).unwrap();
        assert!(
            !q.sql_text.contains("new@example.com"),
            "assignment value not in sql_text"
        );
        assert!(!q.sql_text.contains("42"), "filter value not in sql_text");
        assert!(q.sql_text.contains("$1"));
        assert!(q.sql_text.contains("$2"));
        assert!(q.sql_text.contains("RETURNING"));
        assert_eq!(q.bound_params.len(), 2);
    }

    // ── DELETE tests ─────────────────────────────────────────────────────────

    fn delete_ir(filters: Vec<Filter>) -> QuerySetIR {
        QuerySetIR {
            version: IR_VERSION,
            model_name: "User".into(),
            operation: Operation::Delete { danger: false },
            filters,
            order_by: vec![],
            limit: None,
            offset: None,
            vector_order_by: None,
        }
    }

    /// Unfiltered DELETE must be rejected with `MissingFilter` — unscoped-mutation guard.
    #[test]
    fn emit_delete_rejects_empty_filters() {
        let meta = make_metadata();
        let ir = delete_ir(vec![]);
        let err = emit_delete(&meta, &ir).unwrap_err();
        assert!(
            matches!(err, CompileError::MissingFilter { .. }),
            "expected MissingFilter, got {err:?}"
        );
    }

    #[test]
    fn emit_delete_with_filter_uses_placeholder() {
        let meta = make_metadata();
        let ir = delete_ir(vec![Filter {
            field: FieldRef {
                name: "id".into(),
                index: 0,
            },
            operator: "eq".into(),
            value: BindValue::Int(7),
        }]);
        let q = emit_delete(&meta, &ir).unwrap();
        // Filter value must not appear in sql_text.
        assert!(
            !q.sql_text.contains('7'),
            "filter value must not be in sql_text"
        );
        assert!(q.sql_text.contains("$1"), "placeholder must be in sql_text");
        assert!(
            q.sql_text.starts_with("DELETE FROM"),
            "must start with DELETE FROM"
        );
        assert!(q.sql_text.contains("WHERE"), "WHERE clause required");
        // No RETURNING clause for DELETE.
        assert!(
            !q.sql_text.contains("RETURNING"),
            "DELETE must not have RETURNING"
        );
        assert_eq!(q.bound_params.len(), 1);
        assert!(matches!(q.bound_params[0], BindValue::Int(7)));
    }
}
