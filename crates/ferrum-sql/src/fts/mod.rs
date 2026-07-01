//! Per-dialect full-text search SQL emission.

mod mssql;
mod mysql;
mod postgres;
mod sqlite;

use ferrum_core::{
    compile::FTS_OPERATORS,
    error::CompileError,
    ir::{
        metadata::{FieldMeta, FullTextIndexMeta},
        BindValue, ModelMetadata, TextRankBy, TextSearchMode,
    },
};

use crate::dialect::Dialect;

/// True when ``operator`` is a full-text match operator handled by this module.
#[must_use]
pub fn is_fts_operator(operator: &str) -> bool {
    FTS_OPERATORS.contains(&operator)
}

/// Resolve the FTS5 virtual-table / index name for ``field_index``.
#[must_use]
pub fn fts_index_for_field(
    metadata: &ModelMetadata,
    field_index: usize,
) -> Option<&FullTextIndexMeta> {
    let field_name = &metadata.fields[field_index].name;
    metadata
        .full_text_indexes
        .iter()
        .find(|idx| idx.fields.iter().any(|f| f == field_name))
}

/// Column names (DB) to use in ``MATCH(col1, …)`` for a field.
#[must_use]
pub fn fts_match_columns(metadata: &ModelMetadata, field_index: usize) -> Vec<String> {
    if let Some(idx) = fts_index_for_field(metadata, field_index) {
        return idx
            .fields
            .iter()
            .filter_map(|fname| {
                metadata
                    .fields
                    .iter()
                    .find(|f| &f.name == fname)
                    .map(|f| f.column_name.clone())
            })
            .collect();
    }
    let field = &metadata.fields[field_index];
    if let Some(cols) = &field.fts_source_columns {
        return cols.clone();
    }
    vec![field.column_name.clone()]
}

/// Effective FTS language/regconfig: field-level overrides index-level.
#[must_use]
pub fn effective_fts_config<'a>(
    metadata: &'a ModelMetadata,
    field_index: usize,
    field_meta: &'a FieldMeta,
) -> Option<&'a str> {
    if let Some(cfg) = field_meta.fts_config.as_deref() {
        return Some(cfg);
    }
    fts_index_for_field(metadata, field_index).and_then(|idx| idx.config.as_deref())
}

/// Emit a single FTS filter fragment and optional bound parameter.
#[must_use]
pub fn emit_match(
    dialect: Dialect,
    metadata: &ModelMetadata,
    field_index: usize,
    col: &str,
    operator: &str,
    param_index: usize,
    value: BindValue,
) -> (String, Option<BindValue>) {
    let placeholder = dialect.placeholder(param_index);
    let field_meta = &metadata.fields[field_index];
    match dialect {
        Dialect::Postgres => postgres::emit_match(col, operator, &placeholder, value, field_meta),
        Dialect::Mysql => mysql::emit_match(
            metadata,
            field_index,
            operator,
            &placeholder,
            value,
            field_meta,
        ),
        Dialect::Sqlite => sqlite::emit_match(
            dialect,
            metadata,
            field_index,
            operator,
            &placeholder,
            value,
        ),
        Dialect::Mssql => mssql::emit_match(col, operator, &placeholder, value, field_meta),
    }
}

/// Emit an ``ORDER BY`` relevance expression (without the ``ORDER BY`` keyword).
///
/// # Errors
///
/// Returns [`CompileError::UnknownField`] when `text_rank_by.field` is not in metadata.
pub fn emit_rank_order(
    dialect: Dialect,
    metadata: &ModelMetadata,
    text_rank_by: &TextRankBy,
    table: &str,
    qualify_columns: bool,
    param_index: usize,
) -> Result<String, CompileError> {
    let field_meta = metadata
        .fields
        .get(text_rank_by.field.index)
        .ok_or_else(|| CompileError::UnknownField {
            model: metadata.model_name.clone(),
            field: text_rank_by.field.name.clone(),
        })?;
    let col = if qualify_columns {
        format!(
            "{}.{}",
            dialect.quote_ident(
                table
                    .trim_matches('"')
                    .trim_matches('`')
                    .trim_matches(['[', ']'])
            ),
            dialect.quote_ident(&field_meta.column_name)
        )
    } else {
        dialect.quote_ident(&field_meta.column_name)
    };
    Ok(emit_rank_order_for_column(
        dialect,
        metadata,
        text_rank_by.field.index,
        &col,
        text_rank_by,
        field_meta,
        table,
        param_index,
    ))
}

/// Like [`emit_rank_order`] but accepts a pre-qualified column reference.
#[must_use]
#[allow(clippy::too_many_arguments)]
pub fn emit_rank_order_for_column(
    dialect: Dialect,
    metadata: &ModelMetadata,
    field_index: usize,
    col: &str,
    text_rank_by: &TextRankBy,
    field_meta: &FieldMeta,
    table: &str,
    param_index: usize,
) -> String {
    let placeholder = dialect.placeholder(param_index);
    match dialect {
        Dialect::Postgres => postgres::emit_rank_order(
            col,
            &placeholder,
            text_rank_by.mode,
            field_meta,
            effective_fts_config(metadata, field_index, field_meta),
        ),
        Dialect::Mysql => mysql::emit_rank_order(
            metadata,
            field_index,
            &placeholder,
            text_rank_by.mode,
            field_meta,
        ),
        Dialect::Sqlite => sqlite::emit_rank_order(
            dialect,
            metadata,
            field_index,
            table,
            &placeholder,
            text_rank_by.mode,
        ),
        Dialect::Mssql => mssql::emit_rank_order(
            dialect,
            metadata,
            field_index,
            col,
            table,
            &placeholder,
            text_rank_by.mode,
            field_meta,
        ),
    }
}

/// Map a filter operator string to the IR ``TextSearchMode`` used for ranking.
#[must_use]
pub fn operator_to_mode(operator: &str) -> TextSearchMode {
    match operator {
        "match_phrase" => TextSearchMode::Phrase,
        "match_websearch" => TextSearchMode::Websearch,
        "match_boolean" => TextSearchMode::Boolean,
        _ => TextSearchMode::Plain,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use ferrum_core::ir::{
        metadata::{FieldMeta, FieldType, ModelMetadata},
        BindValue, FieldRef, TextRankBy,
    };

    fn tsvector_field() -> FieldMeta {
        FieldMeta {
            name: "search_vector".into(),
            column_name: "search_vector".into(),
            field_type: FieldType::TsVector,
            allowed_operators: vec!["match".into()],
            nullable: true,
            vector_dimensions: None,
            fts_config: Some("english".into()),
            fts_source_columns: None,
        }
    }

    #[test]
    fn postgres_match_uses_plainto_tsquery_with_config() {
        let meta = ModelMetadata {
            model_name: "Doc".into(),
            table_name: "docs".into(),
            fields: vec![tsvector_field()],
            pk_index: 0,
            pk_fields: vec![0],
            full_text_indexes: vec![],
        };
        let (sql, param) = emit_match(
            Dialect::Postgres,
            &meta,
            0,
            "\"search_vector\"",
            "match",
            1,
            BindValue::Text("hello".into()),
        );
        assert!(sql.contains("@@ plainto_tsquery('english', $1)"));
        assert!(matches!(param, Some(BindValue::Text(_))));
    }

    #[test]
    fn mysql_match_uses_natural_language_mode() {
        let meta = ModelMetadata {
            model_name: "Doc".into(),
            table_name: "docs".into(),
            fields: vec![FieldMeta {
                name: "body".into(),
                column_name: "body".into(),
                field_type: FieldType::Text,
                allowed_operators: vec!["match".into()],
                nullable: false,
                vector_dimensions: None,
                fts_config: None,
                fts_source_columns: None,
            }],
            pk_index: 0,
            pk_fields: vec![0],
            full_text_indexes: vec![FullTextIndexMeta {
                name: "ft_docs_body".into(),
                fields: vec!["body".into()],
                config: None,
            }],
        };
        let (sql, _) = emit_match(
            Dialect::Mysql,
            &meta,
            0,
            "`body`",
            "match",
            1,
            BindValue::Text("hello".into()),
        );
        assert!(sql.contains("MATCH(`body`)"));
        assert!(sql.contains("NATURAL LANGUAGE MODE"));
    }

    #[test]
    fn emit_rank_order_postgres_uses_ts_rank() {
        let meta = ModelMetadata {
            model_name: "Doc".into(),
            table_name: "docs".into(),
            fields: vec![tsvector_field()],
            pk_index: 0,
            pk_fields: vec![0],
            full_text_indexes: vec![],
        };
        let rank = TextRankBy {
            field: FieldRef {
                name: "search_vector".into(),
                index: 0,
            },
            query: BindValue::Text("rust".into()),
            mode: TextSearchMode::Plain,
        };
        let sql = emit_rank_order(Dialect::Postgres, &meta, &rank, "docs", false, 1).unwrap();
        assert!(sql.contains("ts_rank"));
        assert!(sql.contains("plainto_tsquery"));
    }
}
