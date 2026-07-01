//! `SQL Server` full-text search SQL emission.

use ferrum_core::ir::{metadata::FieldMeta, TextSearchMode};

use super::fts_match_columns;
use crate::dialect::Dialect;

fn use_freetext(operator: &str) -> bool {
    matches!(operator, "match" | "match_websearch")
}

fn use_freetext_mode(mode: TextSearchMode) -> bool {
    matches!(mode, TextSearchMode::Plain | TextSearchMode::Websearch)
}

/// Emit ``CONTAINS`` / ``FREETEXT`` filter for SQL Server.
pub fn emit_match(
    col: &str,
    operator: &str,
    placeholder: &str,
    value: ferrum_core::ir::BindValue,
    _field_meta: &FieldMeta,
) -> (String, Option<ferrum_core::ir::BindValue>) {
    let fn_name = if use_freetext(operator) {
        "FREETEXT"
    } else {
        "CONTAINS"
    };
    (format!("{fn_name}({col}, {placeholder})"), Some(value))
}

/// Emit rank via correlated ``CONTAINSTABLE`` / ``FREETEXTTABLE`` subquery.
#[allow(clippy::too_many_arguments)]
pub fn emit_rank_order(
    dialect: Dialect,
    metadata: &ferrum_core::ir::ModelMetadata,
    field_index: usize,
    _col: &str,
    table: &str,
    placeholder: &str,
    mode: TextSearchMode,
    _field_meta: &FieldMeta,
) -> String {
    let table_q = dialect.quote_ident(table);
    let pk_idx = metadata.effective_pk_fields()[0];
    let pk_col = dialect.quote_ident(&metadata.fields[pk_idx].column_name);
    let cols = fts_match_columns(metadata, field_index)
        .iter()
        .map(|c| dialect.quote_ident(c))
        .collect::<Vec<_>>()
        .join(", ");
    let tvf = if use_freetext_mode(mode) {
        "FREETEXTTABLE"
    } else {
        "CONTAINSTABLE"
    };
    format!(
        "(SELECT MAX(ft.[RANK]) FROM {tvf}({table_q}, ({cols}), {placeholder}) AS ft WHERE ft.[KEY] = {table_q}.{pk_col}) DESC"
    )
}
