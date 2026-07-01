//! `MySQL` full-text search SQL emission.

use ferrum_core::ir::{metadata::FieldMeta, BindValue, TextSearchMode};

use super::fts_match_columns;
use crate::dialect::Dialect;

fn mysql_mode(operator: &str) -> &'static str {
    match operator {
        "match_boolean" => "BOOLEAN",
        _ => "NATURAL LANGUAGE",
    }
}

fn mysql_mode_from_ir(mode: TextSearchMode) -> &'static str {
    match mode {
        TextSearchMode::Boolean => "BOOLEAN",
        _ => "NATURAL LANGUAGE",
    }
}

fn match_columns_sql(
    dialect: Dialect,
    metadata: &ferrum_core::ir::ModelMetadata,
    field_index: usize,
) -> String {
    fts_match_columns(metadata, field_index)
        .iter()
        .map(|c| dialect.quote_ident(c))
        .collect::<Vec<_>>()
        .join(", ")
}

/// Emit ``MATCH(cols) AGAINST(? IN … MODE)`` for `MySQL`.
pub fn emit_match(
    metadata: &ferrum_core::ir::ModelMetadata,
    field_index: usize,
    operator: &str,
    placeholder: &str,
    value: BindValue,
    _field_meta: &FieldMeta,
) -> (String, Option<BindValue>) {
    let cols = match_columns_sql(Dialect::Mysql, metadata, field_index);
    let mode = mysql_mode(operator);
    (
        format!("MATCH({cols}) AGAINST ({placeholder} IN {mode} MODE)"),
        Some(value),
    )
}

/// Emit relevance ordering via ``MATCH … AGAINST(?)`` for `MySQL`.
pub fn emit_rank_order(
    metadata: &ferrum_core::ir::ModelMetadata,
    field_index: usize,
    placeholder: &str,
    mode: TextSearchMode,
    _field_meta: &FieldMeta,
) -> String {
    let cols = match_columns_sql(Dialect::Mysql, metadata, field_index);
    let sql_mode = mysql_mode_from_ir(mode);
    format!("MATCH({cols}) AGAINST ({placeholder} IN {sql_mode} MODE) DESC")
}
