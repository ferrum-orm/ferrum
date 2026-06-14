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
        metadata::FieldType, BindValue, ModelMetadata, Operation, QuerySetIR, SortDirection,
        IR_VERSION,
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
            if ir.filters.is_empty() && !danger {
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
            if ir.filters.is_empty() && !danger {
                return Err(CompileError::MissingFilter {
                    model: metadata.model_name.clone(),
                    operation: "delete".into(),
                });
            }
        }
    }

    // Validate all field references in filters before touching SQL.
    for filter in &ir.filters {
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
    }

    // Validate ORDER BY field indices and sort directions before touching SQL.
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

    validate_vector_order_by(metadata, ir)?;

    // Validation passed. Return empty CompiledQuery — sql_text and bound_params
    // are populated by the SQL emitter in `ferrum-sql::emit`.
    Ok(CompiledQuery {
        sql_text: String::new(),
        bound_params: Vec::new(),
        param_type_summary: Vec::new(),
        fingerprint: String::new(),
    })
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
    if !matches!(vector_order.value, BindValue::FloatArray(_)) {
        return Err(CompileError::MalformedIr {
            reason: "vector_order_by.value must be float_array".into(),
        });
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
        };
        assert!(matches!(
            compile(&meta, &ir).unwrap_err(),
            CompileError::MissingFilter { .. }
        ));
    }
}
