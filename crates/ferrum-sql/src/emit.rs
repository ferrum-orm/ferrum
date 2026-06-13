//! SQL emitters: SELECT / INSERT / UPDATE / DELETE / DDL.
//!
//! Each emitter takes validated IR nodes (field refs already checked against
//! allowlists in `ferrum-core::compile`) and produces parameterized SQL text.
//!
//! Placeholder: emitters will be implemented as IR compilation lands in ferrum-core.

use crate::dialect;
use ferrum_core::{
    compile::CompiledQuery,
    error::CompileError,
    ir::{ModelMetadata, QuerySetIR},
};
use std::fmt::Write as _;

/// Emit a SELECT statement from a validated IR.
///
/// # Errors
/// Propagates `CompileError` from allowlist validation.
pub fn emit_select(
    metadata: &ModelMetadata,
    ir: &QuerySetIR,
) -> Result<CompiledQuery, CompileError> {
    // First validate via ferrum-core (allowlist check).
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

    let mut bound_params = Vec::new();
    let mut param_type_summary = Vec::new();
    let mut where_clauses = Vec::new();

    for filter in &ir.filters {
        let col = dialect::quote_ident(&metadata.fields[filter.field.index].column_name);
        let placeholder = dialect::placeholder(bound_params.len() + 1);
        // Only safe operators from the allowlist reach this point.
        let sql_op = operator_to_sql(&filter.operator);
        where_clauses.push(format!("{col} {sql_op} {placeholder}"));
        param_type_summary.push(format!("{}:{}", filter.field.name, filter.operator));
        bound_params.push(filter.value.clone());
    }

    let mut sql = format!("SELECT {fields} FROM {table}");
    if !where_clauses.is_empty() {
        sql.push_str(" WHERE ");
        sql.push_str(&where_clauses.join(" AND "));
    }

    if !ir.order_by.is_empty() {
        let order_parts: Vec<String> = ir
            .order_by
            .iter()
            .map(|o| {
                let col = dialect::quote_ident(&metadata.fields[o.field.index].column_name);
                let dir = match o.direction {
                    ferrum_core::ir::SortDirection::Asc => "ASC",
                    ferrum_core::ir::SortDirection::Desc => "DESC",
                };
                format!("{col} {dir}")
            })
            .collect();
        sql.push_str(" ORDER BY ");
        sql.push_str(&order_parts.join(", "));
    }

    if let Some(limit) = ir.limit {
        write!(sql, " LIMIT {limit}").expect("write to String is infallible");
    }
    if let Some(offset) = ir.offset {
        write!(sql, " OFFSET {offset}").expect("write to String is infallible");
    }

    Ok(CompiledQuery {
        sql_text: sql,
        bound_params,
        param_type_summary,
    })
}

fn operator_to_sql(op: &str) -> &'static str {
    match op {
        "ne" => "!=",
        "gt" => ">",
        "gte" => ">=",
        "lt" => "<",
        "lte" => "<=",
        "is_null" => "IS NULL",
        "is_not_null" => "IS NOT NULL",
        "icontains" => "ILIKE",
        "contains" => "LIKE",
        _ => "=", // covers "eq"; unreachable for other values: allowlist check precedes emission
    }
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

    #[test]
    fn emit_basic_select() {
        let meta = make_metadata();
        let ir = QuerySetIR {
            version: IR_VERSION,
            model_name: "User".into(),
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
        };
        let q = emit_select(&meta, &ir).unwrap();
        assert!(q.sql_text.contains("\"users\""));
        assert!(q.sql_text.contains("\"id\""));
        assert!(q.bound_params.is_empty());
    }

    #[test]
    fn emit_select_with_filter() {
        let meta = make_metadata();
        let ir = QuerySetIR {
            version: IR_VERSION,
            model_name: "User".into(),
            operation: Operation::Select {
                fields: vec![FieldRef {
                    name: "id".into(),
                    index: 0,
                }],
            },
            filters: vec![Filter {
                field: FieldRef {
                    name: "email".into(),
                    index: 1,
                },
                operator: "eq".into(),
                value: BindValue::Text("x@example.com".into()),
            }],
            order_by: vec![],
            limit: None,
            offset: None,
        };
        let q = emit_select(&meta, &ir).unwrap();
        // Bound value must NOT appear in SQL text (SQL-2).
        assert!(!q.sql_text.contains("x@example.com"));
        assert_eq!(q.bound_params.len(), 1);
        assert!(q.sql_text.contains("$1"));
    }

    #[test]
    fn emit_select_with_order_and_limit() {
        let meta = make_metadata();
        let ir = QuerySetIR {
            version: IR_VERSION,
            model_name: "User".into(),
            operation: Operation::Select {
                fields: vec![FieldRef {
                    name: "id".into(),
                    index: 0,
                }],
            },
            filters: vec![],
            order_by: vec![OrderBy {
                field: FieldRef {
                    name: "id".into(),
                    index: 0,
                },
                direction: SortDirection::Desc,
            }],
            limit: Some(10),
            offset: Some(5),
        };
        let q = emit_select(&meta, &ir).unwrap();
        assert!(q.sql_text.contains("ORDER BY"));
        assert!(q.sql_text.contains("DESC"));
        assert!(q.sql_text.contains("LIMIT 10"));
        assert!(q.sql_text.contains("OFFSET 5"));
    }
}
