//! `PostgreSQL` full-text search SQL emission.

use ferrum_core::ir::{metadata::FieldMeta, BindValue, TextSearchMode};

fn tsquery_fn(operator: &str) -> &'static str {
    match operator {
        "match_phrase" => "phraseto_tsquery",
        "match_websearch" => "websearch_to_tsquery",
        "match_boolean" => "to_tsquery",
        _ => "plainto_tsquery",
    }
}

fn tsquery_fn_mode(mode: TextSearchMode) -> &'static str {
    match mode {
        TextSearchMode::Plain => "plainto_tsquery",
        TextSearchMode::Phrase => "phraseto_tsquery",
        TextSearchMode::Websearch => "websearch_to_tsquery",
        TextSearchMode::Boolean => "to_tsquery",
    }
}

fn tsquery_expr(fn_name: &str, config: Option<&str>, placeholder: &str) -> String {
    if let Some(cfg) = config {
        format!("{fn_name}('{cfg}', {placeholder})")
    } else {
        format!("{fn_name}({placeholder})")
    }
}

/// Emit a ``tsvector`` filter predicate for `PostgreSQL`.
pub fn emit_match(
    col: &str,
    operator: &str,
    placeholder: &str,
    value: BindValue,
    field_meta: &FieldMeta,
) -> (String, Option<BindValue>) {
    let tsquery = tsquery_expr(
        tsquery_fn(operator),
        field_meta.fts_config.as_deref(),
        placeholder,
    );
    (format!("{col} @@ {tsquery}"), Some(value))
}

/// Emit ``ORDER BY ts_rank(...) DESC`` for `PostgreSQL`.
pub fn emit_rank_order(
    col: &str,
    placeholder: &str,
    mode: TextSearchMode,
    field_meta: &FieldMeta,
    config: Option<&str>,
) -> String {
    let cfg = config.or(field_meta.fts_config.as_deref());
    let tsquery = tsquery_expr(tsquery_fn_mode(mode), cfg, placeholder);
    format!("ts_rank({col}, {tsquery}) DESC")
}
