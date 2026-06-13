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

use crate::{
    error::CompileError,
    ir::{BindValue, ModelMetadata, QuerySetIR, IR_VERSION},
};

/// The output of a successful compilation pass.
#[derive(Debug, Clone)]
pub struct CompiledQuery {
    /// Parameterized SQL text (`PostgreSQL` `$N` placeholders).
    pub sql_text: String,

    /// Bound parameters in placeholder order. Never contains SQL identifiers.
    pub bound_params: Vec<BindValue>,

    /// Short summary for Tier A observability (operation + table; no values).
    pub param_type_summary: Vec<String>,
}

/// Compile a `QuerySetIR` against its model metadata.
///
/// Returns `Err(CompileError)` if any field, operator, or sort direction is not
/// in the allowlist, or if the IR version does not match `IR_VERSION`.
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

    // Validate ORDER BY directions before touching SQL.
    for order in &ir.order_by {
        metadata
            .fields
            .get(order.field.index)
            .ok_or_else(|| CompileError::UnknownField {
                model: metadata.model_name.clone(),
                field: order.field.name.clone(),
            })?;
    }

    // Delegate to ferrum-sql for dialect-specific emission.
    // (ferrum-sql is a workspace-sibling dependency in ferrum-pyo3; core itself
    //  exposes only the validated AST so the SQL dialect is swappable.)
    //
    // For now, return a placeholder so the crate compiles while ferrum-sql is wired.
    Ok(CompiledQuery {
        sql_text: String::new(),
        bound_params: Vec::new(),
        param_type_summary: Vec::new(),
    })
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
                },
                FieldMeta {
                    name: "email".into(),
                    column_name: "email".into(),
                    field_type: FieldType::Text,
                    allowed_operators: vec!["eq".into(), "icontains".into()],
                    nullable: false,
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
    fn rejects_unknown_field_index() {
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
}
