//! `SQLite` FTS5 SQL emission.

use ferrum_core::ir::{BindValue, TextSearchMode};

use super::fts_index_for_field;
use crate::dialect::Dialect;

fn default_fts_table(metadata: &ferrum_core::ir::ModelMetadata, field_index: usize) -> String {
    let field = &metadata.fields[field_index];
    format!("{}_{}_fts", metadata.table_name, field.name)
}

fn fts_table_name(metadata: &ferrum_core::ir::ModelMetadata, field_index: usize) -> String {
    fts_index_for_field(metadata, field_index).map_or_else(
        || default_fts_table(metadata, field_index),
        |idx| idx.name.clone(),
    )
}

/// Emit FTS5 ``MATCH`` filter via ``rowid`` subquery.
pub fn emit_match(
    dialect: Dialect,
    metadata: &ferrum_core::ir::ModelMetadata,
    field_index: usize,
    _operator: &str,
    placeholder: &str,
    value: BindValue,
) -> (String, Option<BindValue>) {
    let table = dialect.quote_ident(&metadata.table_name);
    let fts = dialect.quote_ident(&fts_table_name(metadata, field_index));
    (
        format!("{table}.rowid IN (SELECT rowid FROM {fts} WHERE {fts} MATCH {placeholder})"),
        Some(value),
    )
}

/// Emit ``bm25(fts) ASC`` rank (lower bm25 = better match in FTS5).
pub fn emit_rank_order(
    dialect: Dialect,
    metadata: &ferrum_core::ir::ModelMetadata,
    field_index: usize,
    table: &str,
    placeholder: &str,
    _mode: TextSearchMode,
) -> String {
    let base = dialect.quote_ident(table);
    let fts = dialect.quote_ident(&fts_table_name(metadata, field_index));
    format!(
        "(SELECT bm25({fts}) FROM {fts} WHERE {fts}.rowid = {base}.rowid AND {fts} MATCH {placeholder}) ASC"
    )
}
