//! IR → `CompiledQuery` transformation.
//!
//! This module validates the `QuerySetIR` against model-metadata allowlists and
//! produces a `CompiledQuery` containing the SQL text and the out-of-band bound
//! parameter list.
//!
//! # Security invariants (SQL-1 / SQL-2)
//! - Field names are resolved *only* via `ModelMetadata::fields` indices. Unknown
//!   field names or indices that exceed the allowlist are a `CompileError` before
//!   any SQL text is touched.
//! - Operator strings are validated against `FieldMeta::allowed_operators` before
//!   they are written into SQL.
//! - Bound parameter values are placed in `bound_params`; they appear in SQL text
//!   only as positional placeholders `$1`, `$2`, …
//! - Sort directions are enum-checked before SQL is produced; the `Unknown` variant
//!   produced by serde for unrecognized strings is rejected here (Defense in Depth).

use crate::{
    error::CompileError,
    ir::{
        metadata::FieldType, BindValue, ModelMetadata, Operation, Predicate, QuerySetIR,
        SortDirection, IR_VERSION,
    },
};

/// The output of a successful compilation pass.
#[derive(Debug, Clone)]
pub struct CompiledQuery {
    /// Parameterized SQL text (`PostgreSQL` `$N` placeholders).
    pub sql_text: String,

    /// Bound parameters in placeholder order. Never contains SQL identifiers.
    pub bound_params: Vec<BindValue>,

    /// Per-parameter PG type summary for Tier A observability (no values).
    pub param_type_summary: Vec<String>,

    /// Stable hash of the SQL shape (identifiers + placeholder positions, no
    /// values). Used as the Tier A observability key and as a future plan-cache
    /// key. Populated by the SQL emitter (`ferrum-sql`); empty string when
    /// returned by the validation-only `compile()` path.
    pub fingerprint: String,
}

/// Validate a `QuerySetIR` against its model metadata.
///
/// This is the allowlist-enforcement layer (Layer 2 in ARCHITECTURE §11.1).
/// It rejects unknown fields, unsupported operators, and invalid sort directions
/// **before** any SQL text is produced. A successful call returns a `CompiledQuery`
/// with an empty `sql_text`; the SQL emitter in `ferrum-sql` fills it in after
/// calling this for validation.
///
/// # Errors
/// See [`CompileError`] variants.
pub fn compile(metadata: &ModelMetadata, ir: &QuerySetIR) -> Result<CompiledQuery, CompileError> {
    if ir.version != IR_VERSION {
        return Err(CompileError::IrVersionMismatch {
            expected: IR_VERSION,
            got: ir.version,
        });
    }

    // Validate operation-specific fields against the allowlist.
    match &ir.operation {
        Operation::Select { fields } => {
            for field_ref in fields {
                metadata
                    .fields
                    .get(field_ref.index)
                    .ok_or_else(|| CompileError::UnknownField {
                        model: metadata.model_name.clone(),
                        field: field_ref.name.clone(),
                    })?;
            }
        }
        Operation::Insert { values } => {
            for (field_ref, _value) in values {
                metadata
                    .fields
                    .get(field_ref.index)
                    .ok_or_else(|| CompileError::UnknownField {
                        model: metadata.model_name.clone(),
                        field: field_ref.name.clone(),
                    })?;
            }
        }
        Operation::Update {
            assignments,
            danger,
        } => {
            // Unscoped UPDATE is a security gate. `danger=true` is only set by
            // `QuerySet.danger_update_all()` — the Python layer has already
            // enforced the explicit caller opt-in before reaching Rust.
            if !ir_has_where_clause(ir) && !danger {
                return Err(CompileError::MissingFilter {
                    model: metadata.model_name.clone(),
                    operation: "update".into(),
                });
            }
            for (field_ref, _value) in assignments {
                metadata
                    .fields
                    .get(field_ref.index)
                    .ok_or_else(|| CompileError::UnknownField {
                        model: metadata.model_name.clone(),
                        field: field_ref.name.clone(),
                    })?;
            }
        }
        Operation::Delete { danger } => {
            // Unscoped DELETE is a security gate. `danger=true` is only set by
            // `QuerySet.danger_delete_all()` — the Python layer has already
            // enforced the explicit caller opt-in before reaching Rust.
            if !ir_has_where_clause(ir) && !danger {
                return Err(CompileError::MissingFilter {
                    model: metadata.model_name.clone(),
                    operation: "delete".into(),
                });
            }
        }
        Operation::BulkInsert { rows, .. } => validate_bulk_insert(metadata, rows)?,
        Operation::BulkUpdate {
            pk_fields,
            fields,
            rows,
        } => validate_bulk_update(metadata, pk_fields, fields, rows)?,
        Operation::BulkDelete { pk_fields, ids } => validate_bulk_delete(metadata, pk_fields, ids)?,
    }

    // Validate all field references in filters before touching SQL.
    for filter in &ir.filters {
        validate_filter(metadata, filter)?;
    }

    if let Some(predicate) = &ir.predicate {
        validate_predicate(metadata, predicate)?;
    }

    validate_joins(metadata, ir)?;
    validate_order_by(metadata, ir)?;
    validate_vector_order_by(metadata, ir)?;
    validate_text_search(metadata, ir)?;

    // Validation passed. Return empty CompiledQuery — sql_text and bound_params
    // are populated by the SQL emitter in `ferrum-sql::emit`.
    Ok(CompiledQuery {
        sql_text: String::new(),
        bound_params: Vec::new(),
        param_type_summary: Vec::new(),
        fingerprint: String::new(),
    })
}

fn validate_joins(metadata: &ModelMetadata, ir: &QuerySetIR) -> Result<(), CompileError> {
    for join in &ir.joins {
        metadata
            .fields
            .get(join.local_field.index)
            .ok_or_else(|| CompileError::UnknownField {
                model: metadata.model_name.clone(),
                field: join.local_field.name.clone(),
            })?;
        if join.remote_table.is_empty() || join.alias.is_empty() {
            return Err(CompileError::MalformedIr {
                reason: "join spec missing remote_table or alias".into(),
            });
        }
    }
    Ok(())
}

fn validate_order_by(metadata: &ModelMetadata, ir: &QuerySetIR) -> Result<(), CompileError> {
    for order in &ir.order_by {
        metadata
            .fields
            .get(order.field.index)
            .ok_or_else(|| CompileError::UnknownField {
                model: metadata.model_name.clone(),
                field: order.field.name.clone(),
            })?;
        // The `Unknown` variant is produced by serde when the JSON direction
        // string is neither "asc" nor "desc". Reject it here before SQL exists.
        if order.direction == SortDirection::Unknown {
            return Err(CompileError::InvalidSortDirection {
                model: metadata.model_name.clone(),
                field: order.field.name.clone(),
                direction: "unknown".into(),
            });
        }
    }
    Ok(())
}

fn validate_bulk_insert(
    metadata: &ModelMetadata,
    rows: &[Vec<(crate::ir::FieldRef, BindValue)>],
) -> Result<(), CompileError> {
    if rows.is_empty() {
        return Err(CompileError::MalformedIr {
            reason: "bulk_insert requires at least one row".into(),
        });
    }
    let first_len = rows[0].len();
    for row in rows {
        if row.len() != first_len {
            return Err(CompileError::MalformedIr {
                reason: "bulk_insert rows must share the same columns".into(),
            });
        }
        for (field_ref, _value) in row {
            metadata
                .fields
                .get(field_ref.index)
                .ok_or_else(|| CompileError::UnknownField {
                    model: metadata.model_name.clone(),
                    field: field_ref.name.clone(),
                })?;
        }
    }
    Ok(())
}

fn validate_bulk_update(
    metadata: &ModelMetadata,
    pk_fields: &[crate::ir::FieldRef],
    fields: &[crate::ir::FieldRef],
    rows: &[crate::ir::BulkUpdateRow],
) -> Result<(), CompileError> {
    if rows.is_empty() {
        return Err(CompileError::MalformedIr {
            reason: "bulk_update requires at least one row".into(),
        });
    }
    if fields.is_empty() {
        return Err(CompileError::MalformedIr {
            reason: "bulk_update requires at least one field".into(),
        });
    }
    if pk_fields.is_empty() {
        return Err(CompileError::MalformedIr {
            reason: "bulk_update requires at least one pk_field".into(),
        });
    }
    for pk_field_ref in pk_fields {
        metadata
            .fields
            .get(pk_field_ref.index)
            .ok_or_else(|| CompileError::UnknownField {
                model: metadata.model_name.clone(),
                field: pk_field_ref.name.clone(),
            })?;
    }
    for field_ref in fields {
        metadata
            .fields
            .get(field_ref.index)
            .ok_or_else(|| CompileError::UnknownField {
                model: metadata.model_name.clone(),
                field: field_ref.name.clone(),
            })?;
    }
    for row in rows {
        if row.values.len() != fields.len() {
            return Err(CompileError::MalformedIr {
                reason: "bulk_update row value count must match fields".into(),
            });
        }
        if row.pk_values.len() != pk_fields.len() {
            return Err(CompileError::MalformedIr {
                reason: "bulk_update row pk_values count must match pk_fields".into(),
            });
        }
    }
    Ok(())
}

fn validate_bulk_delete(
    metadata: &ModelMetadata,
    pk_fields: &[crate::ir::FieldRef],
    ids: &[Vec<BindValue>],
) -> Result<(), CompileError> {
    if ids.is_empty() {
        return Err(CompileError::MalformedIr {
            reason: "bulk_delete requires at least one id".into(),
        });
    }
    if pk_fields.is_empty() {
        return Err(CompileError::MalformedIr {
            reason: "bulk_delete requires at least one pk_field".into(),
        });
    }
    for pk_field_ref in pk_fields {
        metadata
            .fields
            .get(pk_field_ref.index)
            .ok_or_else(|| CompileError::UnknownField {
                model: metadata.model_name.clone(),
                field: pk_field_ref.name.clone(),
            })?;
    }
    Ok(())
}

fn validate_vector_order_by(metadata: &ModelMetadata, ir: &QuerySetIR) -> Result<(), CompileError> {
    let Some(vector_order) = &ir.vector_order_by else {
        return Ok(());
    };
    let field_meta = metadata
        .fields
        .get(vector_order.field.index)
        .ok_or_else(|| CompileError::UnknownField {
            model: metadata.model_name.clone(),
            field: vector_order.field.name.clone(),
        })?;
    if field_meta.field_type != FieldType::Vector {
        return Err(CompileError::UnsupportedOperator {
            model: metadata.model_name.clone(),
            field: vector_order.field.name.clone(),
            operator: "nearest_to".into(),
        });
    }
    // Accept both float_array (canonical vector type) and int_array (integer
    // dims encoded by _encode_bind_value when the caller passes a list[int]).
    if !matches!(
        vector_order.value,
        BindValue::FloatArray(_) | BindValue::IntArray(_)
    ) {
        return Err(CompileError::MalformedIr {
            reason: "vector_order_by.value must be float_array or int_array".into(),
        });
    }
    Ok(())
}

/// Ferrum full-text filter operators (validated against ``tsvector`` fields).
pub const FTS_OPERATORS: &[&str] = &["match", "match_phrase", "match_websearch", "match_boolean"];

fn is_valid_fts_identifier(value: &str) -> bool {
    !value.is_empty() && value.chars().all(|c| c.is_ascii_alphanumeric() || c == '_')
}

fn validate_fts_field_metadata(
    field_meta: &crate::ir::metadata::FieldMeta,
) -> Result<(), CompileError> {
    if let Some(config) = &field_meta.fts_config {
        if !is_valid_fts_identifier(config) {
            return Err(CompileError::MalformedIr {
                reason: format!("invalid fts_config identifier: {config:?}"),
            });
        }
    }
    if let Some(cols) = &field_meta.fts_source_columns {
        for col in cols {
            if !is_valid_fts_identifier(col) {
                return Err(CompileError::MalformedIr {
                    reason: format!("invalid fts_source_columns identifier: {col:?}"),
                });
            }
        }
    }
    Ok(())
}

fn field_supports_fts(
    metadata: &ModelMetadata,
    field_meta: &crate::ir::metadata::FieldMeta,
) -> bool {
    if field_meta.field_type == FieldType::TsVector {
        return true;
    }
    if field_meta.field_type == FieldType::Text {
        let in_index = metadata
            .full_text_indexes
            .iter()
            .any(|idx| idx.fields.iter().any(|f| f == &field_meta.name));
        return in_index || field_meta.fts_source_columns.is_some();
    }
    false
}

fn validate_fts_filter(
    metadata: &ModelMetadata,
    filter: &crate::ir::Filter,
) -> Result<(), CompileError> {
    let field_meta =
        metadata
            .fields
            .get(filter.field.index)
            .ok_or_else(|| CompileError::UnknownField {
                model: metadata.model_name.clone(),
                field: filter.field.name.clone(),
            })?;
    if !field_supports_fts(metadata, field_meta) {
        return Err(CompileError::UnsupportedOperator {
            model: metadata.model_name.clone(),
            field: filter.field.name.clone(),
            operator: filter.operator.clone(),
        });
    }
    if !matches!(filter.value, BindValue::Text(_)) {
        return Err(CompileError::MalformedIr {
            reason: format!(
                "FTS operator {} requires a text bind value",
                filter.operator
            ),
        });
    }
    validate_fts_field_metadata(field_meta)
}

fn validate_text_rank_by(
    metadata: &ModelMetadata,
    rank: &crate::ir::TextRankBy,
) -> Result<(), CompileError> {
    let field_meta =
        metadata
            .fields
            .get(rank.field.index)
            .ok_or_else(|| CompileError::UnknownField {
                model: metadata.model_name.clone(),
                field: rank.field.name.clone(),
            })?;
    if !field_supports_fts(metadata, field_meta) {
        return Err(CompileError::UnsupportedOperator {
            model: metadata.model_name.clone(),
            field: rank.field.name.clone(),
            operator: "rank_by".into(),
        });
    }
    if !matches!(rank.query, BindValue::Text(_)) {
        return Err(CompileError::MalformedIr {
            reason: "text_rank_by.query must be a text bind value".into(),
        });
    }
    validate_fts_field_metadata(field_meta)?;
    // Mode is enum-validated at deserialization; no per-query config strings.
    let _ = rank.mode;
    Ok(())
}

fn validate_text_search(metadata: &ModelMetadata, ir: &QuerySetIR) -> Result<(), CompileError> {
    for filter in &ir.filters {
        if FTS_OPERATORS.contains(&filter.operator.as_str()) {
            validate_fts_filter(metadata, filter)?;
        }
    }
    if let Some(predicate) = &ir.predicate {
        validate_predicate_fts(metadata, predicate)?;
    }
    if let Some(rank) = &ir.text_rank_by {
        validate_text_rank_by(metadata, rank)?;
    }
    Ok(())
}

fn validate_predicate_fts(
    metadata: &ModelMetadata,
    predicate: &Predicate,
) -> Result<(), CompileError> {
    match predicate {
        Predicate::And { children } | Predicate::Or { children } => {
            for child in children {
                validate_predicate_fts(metadata, child)?;
            }
        }
        Predicate::Not { child } => validate_predicate_fts(metadata, child)?,
        Predicate::Filter { filter } => {
            if FTS_OPERATORS.contains(&filter.operator.as_str()) {
                validate_fts_filter(metadata, filter)?;
            }
        }
    }
    Ok(())
}

/// True when the IR carries at least one WHERE constraint (flat filters or a predicate tree).
#[must_use]
pub fn ir_has_where_clause(ir: &QuerySetIR) -> bool {
    !ir.filters.is_empty() || ir.predicate.is_some()
}

fn validate_filter(
    metadata: &ModelMetadata,
    filter: &crate::ir::Filter,
) -> Result<(), CompileError> {
    let field_meta =
        metadata
            .fields
            .get(filter.field.index)
            .ok_or_else(|| CompileError::UnknownField {
                model: metadata.model_name.clone(),
                field: filter.field.name.clone(),
            })?;

    if !field_meta.allowed_operators.contains(&filter.operator) {
        return Err(CompileError::UnsupportedOperator {
            model: metadata.model_name.clone(),
            field: filter.field.name.clone(),
            operator: filter.operator.clone(),
        });
    }
    Ok(())
}

fn validate_predicate(metadata: &ModelMetadata, predicate: &Predicate) -> Result<(), CompileError> {
    match predicate {
        Predicate::And { children } | Predicate::Or { children } => {
            for child in children {
                validate_predicate(metadata, child)?;
            }
        }
        Predicate::Not { child } => validate_predicate(metadata, child)?,
        Predicate::Filter { filter } => validate_filter(metadata, filter)?,
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ir::{
        metadata::{FieldMeta, FieldType},
        BindValue, FieldRef, Filter, Operation, OrderBy, QuerySetIR, SortDirection,
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
                    allowed_operators: vec!["eq".into(), "gt".into(), "lt".into()],
                    nullable: false,
                    vector_dimensions: None,
                    fts_config: None,
                    fts_source_columns: None,
                },
                FieldMeta {
                    name: "email".into(),
                    column_name: "email".into(),
                    field_type: FieldType::Text,
                    allowed_operators: vec!["eq".into(), "icontains".into()],
                    nullable: false,
                    vector_dimensions: None,
                    fts_config: None,
                    fts_source_columns: None,
                },
            ],
            pk_index: 0,
            pk_fields: vec![0],
            full_text_indexes: vec![],
        }
    }

    fn base_ir(model_name: &str) -> QuerySetIR {
        QuerySetIR {
            version: IR_VERSION,
            model_name: model_name.into(),
            operation: Operation::Select {
                fields: vec![
                    FieldRef {
                        name: "id".into(),
                        index: 0,
                    },
                    FieldRef {
                        name: "email".into(),
                        index: 1,
                    },
                ],
            },
            filters: vec![],
            order_by: vec![],
            limit: None,
            offset: None,
            vector_order_by: None,
            text_rank_by: None,
            predicate: None,
            distinct: false,
            exists: false,
            joins: vec![],
        }
    }

    #[test]
    fn rejects_wrong_ir_version() {
        let meta = make_metadata();
        let mut ir = base_ir("User");
        ir.version = 999;
        let err = compile(&meta, &ir).unwrap_err();
        assert!(matches!(err, CompileError::IrVersionMismatch { .. }));
    }

    #[test]
    fn rejects_unknown_field_index_in_filter() {
        let meta = make_metadata();
        let mut ir = base_ir("User");
        ir.filters.push(Filter {
            field: FieldRef {
                name: "nonexistent".into(),
                index: 99,
            },
            operator: "eq".into(),
            value: BindValue::Int(1),
        });
        let err = compile(&meta, &ir).unwrap_err();
        assert!(matches!(err, CompileError::UnknownField { .. }));
    }

    #[test]
    fn rejects_unknown_select_field_index() {
        let meta = make_metadata();
        let mut ir = base_ir("User");
        ir.operation = Operation::Select {
            fields: vec![FieldRef {
                name: "ghost".into(),
                index: 99, // out-of-range index
            }],
        };
        let err = compile(&meta, &ir).unwrap_err();
        assert!(matches!(err, CompileError::UnknownField { .. }));
    }

    #[test]
    fn rejects_unsupported_operator() {
        let meta = make_metadata();
        let mut ir = base_ir("User");
        ir.filters.push(Filter {
            field: FieldRef {
                name: "id".into(),
                index: 0,
            },
            operator: "regex".into(), // not in allowed_operators
            value: BindValue::Int(1),
        });
        let err = compile(&meta, &ir).unwrap_err();
        assert!(matches!(err, CompileError::UnsupportedOperator { .. }));
    }

    /// `SortDirection::Unknown` is produced by serde when the JSON direction
    /// string is not "asc" or "desc". The compiler must reject it before SQL.
    #[test]
    fn rejects_invalid_sort_direction() {
        let meta = make_metadata();
        let mut ir = base_ir("User");
        ir.order_by.push(OrderBy {
            field: FieldRef {
                name: "id".into(),
                index: 0,
            },
            direction: SortDirection::Unknown,
        });
        let err = compile(&meta, &ir).unwrap_err();
        assert!(
            matches!(err, CompileError::InvalidSortDirection { .. }),
            "expected InvalidSortDirection, got {err:?}"
        );
    }

    /// Verify that `Unknown` is also what serde produces when deserializing an
    /// unrecognized direction string from JSON (the Python → Rust path).
    #[test]
    fn serde_unknown_direction_deserializes_to_unknown_variant() {
        let json = r#""sideways""#;
        let dir: SortDirection = serde_json::from_str(json).expect("serde should not error");
        assert_eq!(dir, SortDirection::Unknown);
    }

    #[test]
    fn rejects_fts_operator_on_non_tsvector_field() {
        let meta = make_metadata();
        let mut ir = base_ir("User");
        ir.filters.push(Filter {
            field: FieldRef {
                name: "email".into(),
                index: 1,
            },
            operator: "match".into(),
            value: BindValue::Text("hello".into()),
        });
        let err = compile(&meta, &ir).unwrap_err();
        assert!(matches!(err, CompileError::UnsupportedOperator { .. }));
    }

    #[test]
    fn accepts_tsvector_match_and_text_rank_by() {
        let meta = ModelMetadata {
            model_name: "Doc".into(),
            table_name: "docs".into(),
            fields: vec![FieldMeta {
                name: "search_vector".into(),
                column_name: "search_vector".into(),
                field_type: FieldType::TsVector,
                allowed_operators: vec![
                    "match".into(),
                    "match_phrase".into(),
                    "match_websearch".into(),
                    "match_boolean".into(),
                    "is_null".into(),
                ],
                nullable: true,
                vector_dimensions: None,
                fts_config: Some("english".into()),
                fts_source_columns: None,
            }],
            pk_index: 0,
            pk_fields: vec![0],
            full_text_indexes: vec![],
        };
        let mut ir = base_ir("Doc");
        ir.operation = Operation::Select {
            fields: vec![FieldRef {
                name: "search_vector".into(),
                index: 0,
            }],
        };
        ir.filters.push(Filter {
            field: FieldRef {
                name: "search_vector".into(),
                index: 0,
            },
            operator: "match_phrase".into(),
            value: BindValue::Text("hello world".into()),
        });
        ir.text_rank_by = Some(crate::ir::TextRankBy {
            field: FieldRef {
                name: "search_vector".into(),
                index: 0,
            },
            query: BindValue::Text("hello".into()),
            mode: crate::ir::TextSearchMode::Phrase,
        });
        assert!(compile(&meta, &ir).is_ok());
    }

    #[test]
    fn accepts_valid_ir() {
        let meta = make_metadata();
        let mut ir = base_ir("User");
        ir.filters.push(Filter {
            field: FieldRef {
                name: "email".into(),
                index: 1,
            },
            operator: "eq".into(),
            value: BindValue::Text("test@example.com".into()),
        });
        ir.order_by.push(OrderBy {
            field: FieldRef {
                name: "id".into(),
                index: 0,
            },
            direction: SortDirection::Asc,
        });
        assert!(compile(&meta, &ir).is_ok());
    }

    #[test]
    fn query_fingerprint_contains_operation_and_model() {
        let meta = make_metadata();
        let fp = meta.query_fingerprint("Select");
        assert_eq!(fp, "Select:User");
        assert!(!fp.contains('@')); // no values
    }

    #[test]
    fn accepts_valid_insert_ir() {
        let meta = make_metadata();
        let ir = QuerySetIR {
            version: IR_VERSION,
            model_name: "User".into(),
            operation: Operation::Insert {
                values: vec![(
                    FieldRef {
                        name: "email".into(),
                        index: 1,
                    },
                    BindValue::Text("x@example.com".into()),
                )],
            },
            filters: vec![],
            order_by: vec![],
            limit: None,
            offset: None,
            vector_order_by: None,
            text_rank_by: None,
            predicate: None,
            distinct: false,
            exists: false,
            joins: vec![],
        };
        assert!(compile(&meta, &ir).is_ok());
    }

    #[test]
    fn rejects_unknown_field_in_insert() {
        let meta = make_metadata();
        let ir = QuerySetIR {
            version: IR_VERSION,
            model_name: "User".into(),
            operation: Operation::Insert {
                values: vec![(
                    FieldRef {
                        name: "ghost".into(),
                        index: 99,
                    },
                    BindValue::Int(1),
                )],
            },
            filters: vec![],
            order_by: vec![],
            limit: None,
            offset: None,
            vector_order_by: None,
            text_rank_by: None,
            predicate: None,
            distinct: false,
            exists: false,
            joins: vec![],
        };
        assert!(matches!(
            compile(&meta, &ir).unwrap_err(),
            CompileError::UnknownField { .. }
        ));
    }

    #[test]
    fn rejects_delete_with_no_filters() {
        let meta = make_metadata();
        let ir = QuerySetIR {
            version: IR_VERSION,
            model_name: "User".into(),
            operation: Operation::Delete { danger: false },
            filters: vec![],
            order_by: vec![],
            limit: None,
            offset: None,
            vector_order_by: None,
            text_rank_by: None,
            predicate: None,
            distinct: false,
            exists: false,
            joins: vec![],
        };
        assert!(matches!(
            compile(&meta, &ir).unwrap_err(),
            CompileError::MissingFilter { .. }
        ));
    }

    #[test]
    fn rejects_update_with_no_filters() {
        let meta = make_metadata();
        let ir = QuerySetIR {
            version: IR_VERSION,
            model_name: "User".into(),
            operation: Operation::Update {
                assignments: vec![(
                    FieldRef {
                        name: "email".into(),
                        index: 1,
                    },
                    BindValue::Text("new@example.com".into()),
                )],
                danger: false,
            },
            filters: vec![],
            order_by: vec![],
            limit: None,
            offset: None,
            vector_order_by: None,
            text_rank_by: None,
            predicate: None,
            distinct: false,
            exists: false,
            joins: vec![],
        };
        assert!(matches!(
            compile(&meta, &ir).unwrap_err(),
            CompileError::MissingFilter { .. }
        ));
    }
}
