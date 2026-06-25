//! SQL emitters: SELECT, INSERT, UPDATE, DELETE — parameterized per dialect.
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

use crate::dialect::Dialect;
use ferrum_core::{
    compile::CompiledQuery,
    error::CompileError,
    ir::{BindValue, ModelMetadata, Predicate, QuerySetIR, SortDirection, VectorMetric},
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
    dialect: Dialect,
    metadata: &ModelMetadata,
    ir: &QuerySetIR,
) -> Result<CompiledQuery, CompileError> {
    // Run all allowlist validation first — fail before any SQL exists (SQL-1).
    ferrum_core::compile::compile(metadata, ir)?;

    // Build SELECT clause from validated field refs.
    let table = dialect.quote_ident(&metadata.table_name);
    let qualify_columns = !ir.joins.is_empty();
    let mut select_parts: Vec<String> = match &ir.operation {
        ferrum_core::ir::Operation::Select { fields } => fields
            .iter()
            .map(|f| {
                let col = &metadata.fields[f.index].column_name;
                let quoted = dialect.quote_ident(col);
                if ir.joins.is_empty() {
                    quoted
                } else {
                    format!("{table}.{quoted}")
                }
            })
            .collect(),
        _ => {
            return Err(CompileError::MalformedIr {
                reason: "emit_select called with non-Select operation".into(),
            })
        }
    };

    for join in &ir.joins {
        let alias = dialect.quote_ident(&join.alias);
        for rf in &join.remote_fields {
            let col = dialect.quote_ident(&rf.column);
            select_parts.push(format!(
                "{alias}.{col} AS \"{}__{}\"",
                join.alias, rf.column
            ));
        }
    }
    let fields = select_parts.join(", ");

    let mut bound_params: Vec<BindValue> = Vec::new();
    let mut param_type_summary: Vec<String> = Vec::new();

    let where_sql = build_where_sql(
        dialect,
        metadata,
        ir,
        &table,
        qualify_columns,
        &mut bound_params,
        &mut param_type_summary,
    )?;

    if ir.exists {
        let mut inner = format!("SELECT 1 FROM {table}");
        if let Some(where_clause) = where_sql {
            write!(inner, " WHERE {where_clause}").expect("write to String is infallible");
        }
        append_order_limit_offset(
            dialect,
            metadata,
            ir,
            &table,
            qualify_columns,
            &mut inner,
            &mut bound_params,
            &mut param_type_summary,
        )?;
        let sql = format!("SELECT EXISTS({inner})");
        let fingerprint = sql_fingerprint(&sql);
        return Ok(CompiledQuery {
            sql_text: sql,
            bound_params,
            param_type_summary,
            fingerprint,
        });
    }

    let distinct_kw = if ir.distinct { "DISTINCT " } else { "" };
    let mut sql = format!("SELECT {distinct_kw}{fields} FROM {table}");
    for join in &ir.joins {
        let alias = dialect.quote_ident(&join.alias);
        let remote_table = dialect.quote_ident(&join.remote_table);
        let local_col = dialect.quote_ident(&metadata.fields[join.local_field.index].column_name);
        let remote_pk = dialect.quote_ident(&join.remote_pk_column);
        write!(
            sql,
            " LEFT JOIN {remote_table} AS {alias} ON {table}.{local_col} = {alias}.{remote_pk}"
        )
        .expect("write to String is infallible");
    }
    if let Some(where_clause) = where_sql {
        write!(sql, " WHERE {where_clause}").expect("write to String is infallible");
    }

    append_order_limit_offset(
        dialect,
        metadata,
        ir,
        &table,
        qualify_columns,
        &mut sql,
        &mut bound_params,
        &mut param_type_summary,
    )?;

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
    dialect: Dialect,
    metadata: &ModelMetadata,
    ir: &QuerySetIR,
) -> Result<CompiledQuery, CompileError> {
    ferrum_core::compile::compile(metadata, ir)?;

    let ferrum_core::ir::Operation::Insert { values } = &ir.operation else {
        return Err(CompileError::MalformedIr {
            reason: "emit_insert called with non-Insert operation".into(),
        });
    };

    let table = dialect.quote_ident(&metadata.table_name);
    let mut bound_params: Vec<BindValue> = Vec::new();
    let mut param_type_summary: Vec<String> = Vec::new();
    let mut col_names: Vec<String> = Vec::new();
    let mut placeholders: Vec<String> = Vec::new();

    for (field_ref, value) in values {
        // Column name from the metadata allowlist — never the raw user string.
        col_names.push(dialect.quote_ident(&metadata.fields[field_ref.index].column_name));
        let ph = dialect.placeholder(bound_params.len() + 1);
        placeholders.push(ph);
        param_type_summary.push(format!("{}:insert", field_ref.name));
        bound_params.push(value.clone());
    }

    // RETURNING all model fields so the Python side can hydrate the inserted row.
    let returning = returning_all_fields(dialect, metadata);

    let sql = if dialect.supports_returning() {
        format!(
            "INSERT INTO {table} ({cols}) VALUES ({vals}) RETURNING {returning}",
            cols = col_names.join(", "),
            vals = placeholders.join(", "),
        )
    } else {
        format!(
            "INSERT INTO {table} ({cols}) VALUES ({vals})",
            cols = col_names.join(", "),
            vals = placeholders.join(", "),
        )
    };
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
    dialect: Dialect,
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

    let table = dialect.quote_ident(&metadata.table_name);
    let mut bound_params: Vec<BindValue> = Vec::new();
    let mut param_type_summary: Vec<String> = Vec::new();
    let mut set_clauses: Vec<String> = Vec::new();

    for (field_ref, value) in assignments {
        let col = dialect.quote_ident(&metadata.fields[field_ref.index].column_name);
        let ph = dialect.placeholder(bound_params.len() + 1);
        set_clauses.push(format!("{col} = {ph}"));
        param_type_summary.push(format!("{}:assign", field_ref.name));
        bound_params.push(value.clone());
    }

    let mut where_clauses: Vec<String> = Vec::new();
    if let Some(where_sql) = build_where_sql(
        dialect,
        metadata,
        ir,
        &table,
        false,
        &mut bound_params,
        &mut param_type_summary,
    )? {
        where_clauses.push(where_sql);
    }

    let returning = returning_all_fields(dialect, metadata);

    let mut sql = format!("UPDATE {table} SET {}", set_clauses.join(", "));
    if !where_clauses.is_empty() {
        write!(sql, " WHERE {}", where_clauses.join(" AND "))
            .expect("write to String is infallible");
    }
    if dialect.supports_returning() {
        write!(sql, " RETURNING {returning}").expect("write to String is infallible");
    }

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
    dialect: Dialect,
    metadata: &ModelMetadata,
    ir: &QuerySetIR,
) -> Result<CompiledQuery, CompileError> {
    // `compile` enforces MissingFilter for unfiltered DELETE.
    ferrum_core::compile::compile(metadata, ir)?;

    let table = dialect.quote_ident(&metadata.table_name);
    let mut bound_params: Vec<BindValue> = Vec::new();
    let mut param_type_summary: Vec<String> = Vec::new();
    let where_sql = build_where_sql(
        dialect,
        metadata,
        ir,
        &table,
        false,
        &mut bound_params,
        &mut param_type_summary,
    )?;

    let mut sql = format!("DELETE FROM {table}");
    if let Some(where_clause) = where_sql {
        write!(sql, " WHERE {where_clause}").expect("write to String is infallible");
    }
    let fingerprint = sql_fingerprint(&sql);

    Ok(CompiledQuery {
        sql_text: sql,
        bound_params,
        param_type_summary,
        fingerprint,
    })
}

/// Emit a multi-row parameterized INSERT from a `QuerySetIR`.
pub fn emit_bulk_insert(
    dialect: Dialect,
    metadata: &ModelMetadata,
    ir: &QuerySetIR,
) -> Result<CompiledQuery, CompileError> {
    ferrum_core::compile::compile(metadata, ir)?;

    let ferrum_core::ir::Operation::BulkInsert { rows, returning } = &ir.operation else {
        return Err(CompileError::MalformedIr {
            reason: "emit_bulk_insert called with non-BulkInsert operation".into(),
        });
    };

    let table = dialect.quote_ident(&metadata.table_name);
    let first_row = &rows[0];
    let mut col_names: Vec<String> = Vec::new();
    for (field_ref, _) in first_row {
        col_names.push(dialect.quote_ident(&metadata.fields[field_ref.index].column_name));
    }

    let mut bound_params: Vec<BindValue> = Vec::new();
    let mut param_type_summary: Vec<String> = Vec::new();
    let mut value_groups: Vec<String> = Vec::new();

    for row in rows {
        let mut placeholders: Vec<String> = Vec::new();
        for (field_ref, value) in row {
            let ph = dialect.placeholder(bound_params.len() + 1);
            placeholders.push(ph);
            param_type_summary.push(format!("{}:bulk_insert", field_ref.name));
            bound_params.push(value.clone());
        }
        value_groups.push(format!("({})", placeholders.join(", ")));
    }

    let returning_clause = returning_all_fields(dialect, metadata);
    let sql = if *returning && dialect.supports_returning() {
        format!(
            "INSERT INTO {table} ({cols}) VALUES {groups} RETURNING {returning_clause}",
            cols = col_names.join(", "),
            groups = value_groups.join(", "),
        )
    } else {
        format!(
            "INSERT INTO {table} ({cols}) VALUES {groups}",
            cols = col_names.join(", "),
            groups = value_groups.join(", "),
        )
    };
    let fingerprint = sql_fingerprint(&sql);

    Ok(CompiledQuery {
        sql_text: sql,
        bound_params,
        param_type_summary,
        fingerprint,
    })
}

/// Emit a PK-keyed multi-row UPDATE (PostgreSQL ``UPDATE … FROM (VALUES …)``).
pub fn emit_bulk_update(
    dialect: Dialect,
    metadata: &ModelMetadata,
    ir: &QuerySetIR,
) -> Result<CompiledQuery, CompileError> {
    ferrum_core::compile::compile(metadata, ir)?;

    let ferrum_core::ir::Operation::BulkUpdate {
        pk_field,
        fields,
        rows,
    } = &ir.operation
    else {
        return Err(CompileError::MalformedIr {
            reason: "emit_bulk_update called with non-BulkUpdate operation".into(),
        });
    };

    if dialect != Dialect::Postgres {
        return Err(CompileError::MalformedIr {
            reason: "bulk_update is only supported on PostgreSQL".into(),
        });
    }

    let table = dialect.quote_ident(&metadata.table_name);
    let pk_col = dialect.quote_ident(&metadata.fields[pk_field.index].column_name);
    let pk_name = &metadata.fields[pk_field.index].column_name;

    let mut bound_params: Vec<BindValue> = Vec::new();
    let mut param_type_summary: Vec<String> = Vec::new();

    let col_names: Vec<&str> = std::iter::once(pk_name.as_str())
        .chain(
            fields
                .iter()
                .map(|f| metadata.fields[f.index].column_name.as_str()),
        )
        .collect();

    let mut value_rows: Vec<String> = Vec::new();
    for row in rows {
        let mut placeholders: Vec<String> = Vec::new();
        let ph_pk = dialect.placeholder(bound_params.len() + 1);
        placeholders.push(postgres_value_cast(
            metadata.fields[pk_field.index].field_type,
            &ph_pk,
        ));
        param_type_summary.push(format!("{}:bulk_update_pk", pk_field.name));
        bound_params.push(row.pk.clone());
        for (field_ref, value) in fields.iter().zip(row.values.iter()) {
            let ph = dialect.placeholder(bound_params.len() + 1);
            placeholders.push(postgres_value_cast(
                metadata.fields[field_ref.index].field_type,
                &ph,
            ));
            param_type_summary.push(format!("{}:bulk_update", field_ref.name));
            bound_params.push(value.clone());
        }
        value_rows.push(format!("({})", placeholders.join(", ")));
    }

    let set_clauses: Vec<String> = fields
        .iter()
        .map(|f| {
            let col = dialect.quote_ident(&metadata.fields[f.index].column_name);
            let name = &metadata.fields[f.index].column_name;
            format!("{col} = v.{name}")
        })
        .collect();

    let sql = format!(
        "UPDATE {table} AS t SET {sets} FROM (VALUES {rows}) AS v({cols}) WHERE t.{pk_col} = v.{pk_name}",
        sets = set_clauses.join(", "),
        rows = value_rows.join(", "),
        cols = col_names.join(", "),
        pk_name = pk_name,
    );
    let fingerprint = sql_fingerprint(&sql);

    Ok(CompiledQuery {
        sql_text: sql,
        bound_params,
        param_type_summary,
        fingerprint,
    })
}

/// Emit a PK-keyed multi-row DELETE (``WHERE pk IN ($1, $2, …)``).
pub fn emit_bulk_delete(
    dialect: Dialect,
    metadata: &ModelMetadata,
    ir: &QuerySetIR,
) -> Result<CompiledQuery, CompileError> {
    ferrum_core::compile::compile(metadata, ir)?;

    let ferrum_core::ir::Operation::BulkDelete { pk_field, ids } = &ir.operation else {
        return Err(CompileError::MalformedIr {
            reason: "emit_bulk_delete called with non-BulkDelete operation".into(),
        });
    };

    let table = dialect.quote_ident(&metadata.table_name);
    let pk_col = dialect.quote_ident(&metadata.fields[pk_field.index].column_name);
    let mut bound_params: Vec<BindValue> = Vec::new();
    let mut param_type_summary: Vec<String> = Vec::new();
    let mut placeholders: Vec<String> = Vec::new();

    for id in ids {
        let ph = dialect.placeholder(bound_params.len() + 1);
        placeholders.push(ph);
        param_type_summary.push(format!("{}:bulk_delete", pk_field.name));
        bound_params.push(id.clone());
    }

    let sql = format!(
        "DELETE FROM {table} WHERE {pk_col} IN ({placeholders})",
        placeholders = placeholders.join(", "),
    );
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
fn returning_all_fields(dialect: Dialect, metadata: &ModelMetadata) -> String {
    metadata
        .fields
        .iter()
        .map(|f| dialect.quote_ident(&f.column_name))
        .collect::<Vec<_>>()
        .join(", ")
}

fn qualify_base_column(dialect: Dialect, table: &str, column_name: &str, qualify: bool) -> String {
    let col = dialect.quote_ident(column_name);
    if qualify {
        format!("{table}.{col}")
    } else {
        col
    }
}

fn append_order_limit_offset(
    dialect: Dialect,
    metadata: &ModelMetadata,
    ir: &QuerySetIR,
    table: &str,
    qualify_columns: bool,
    sql: &mut String,
    bound_params: &mut Vec<BindValue>,
    param_type_summary: &mut Vec<String>,
) -> Result<(), CompileError> {
    if !ir.order_by.is_empty() {
        let mut order_parts: Vec<String> = Vec::new();
        for o in &ir.order_by {
            let col = qualify_base_column(
                dialect,
                table,
                &metadata.fields[o.field.index].column_name,
                qualify_columns,
            );
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
        write!(sql, " ORDER BY {}", order_parts.join(", ")).expect("write to String is infallible");
    } else if let Some(vector_order) = &ir.vector_order_by {
        let col = qualify_base_column(
            dialect,
            table,
            &metadata.fields[vector_order.field.index].column_name,
            qualify_columns,
        );
        let op = vector_metric_to_sql(vector_order.metric);
        let placeholder = dialect.placeholder(bound_params.len() + 1);
        write!(sql, " ORDER BY {col} {op} {placeholder}").expect("write to String is infallible");
        param_type_summary.push(format!("{}:nearest_to", vector_order.field.name));
        bound_params.push(vector_order.value.clone());
    }

    if let Some(limit) = ir.limit {
        let placeholder = dialect.placeholder(bound_params.len() + 1);
        write!(sql, " LIMIT {placeholder}").expect("write to String is infallible");
        param_type_summary.push("limit:int".into());
        bound_params.push(BindValue::Int(i64::try_from(limit).unwrap_or(i64::MAX)));
    }
    if let Some(offset) = ir.offset {
        let placeholder = dialect.placeholder(bound_params.len() + 1);
        write!(sql, " OFFSET {placeholder}").expect("write to String is infallible");
        param_type_summary.push("offset:int".into());
        bound_params.push(BindValue::Int(i64::try_from(offset).unwrap_or(i64::MAX)));
    }
    Ok(())
}

fn build_where_sql(
    dialect: Dialect,
    metadata: &ModelMetadata,
    ir: &QuerySetIR,
    table: &str,
    qualify_columns: bool,
    bound_params: &mut Vec<BindValue>,
    param_type_summary: &mut Vec<String>,
) -> Result<Option<String>, CompileError> {
    let mut parts: Vec<String> = Vec::new();

    for filter in &ir.filters {
        let col = qualify_base_column(
            dialect,
            table,
            &metadata.fields[filter.field.index].column_name,
            qualify_columns,
        );
        let (clause, param) = filter_clause(
            dialect,
            &col,
            &filter.operator,
            bound_params.len() + 1,
            filter.value.clone(),
        );
        parts.push(clause);
        if let Some(value) = param {
            param_type_summary.push(format!("{}:{}", filter.field.name, filter.operator));
            bound_params.push(value);
        }
    }

    if let Some(predicate) = &ir.predicate {
        parts.push(emit_predicate(
            dialect,
            metadata,
            predicate,
            table,
            qualify_columns,
            bound_params,
            param_type_summary,
        )?);
    }

    if parts.is_empty() {
        Ok(None)
    } else if parts.len() == 1 {
        Ok(Some(parts.remove(0)))
    } else {
        Ok(Some(format!("({})", parts.join(" AND "))))
    }
}

fn emit_predicate(
    dialect: Dialect,
    metadata: &ModelMetadata,
    predicate: &Predicate,
    table: &str,
    qualify_columns: bool,
    bound_params: &mut Vec<BindValue>,
    param_type_summary: &mut Vec<String>,
) -> Result<String, CompileError> {
    match predicate {
        Predicate::And { children } => {
            let subs: Result<Vec<String>, CompileError> = children
                .iter()
                .map(|child| {
                    emit_predicate(
                        dialect,
                        metadata,
                        child,
                        table,
                        qualify_columns,
                        bound_params,
                        param_type_summary,
                    )
                })
                .collect();
            Ok(format!("({})", subs?.join(" AND ")))
        }
        Predicate::Or { children } => {
            let subs: Result<Vec<String>, CompileError> = children
                .iter()
                .map(|child| {
                    emit_predicate(
                        dialect,
                        metadata,
                        child,
                        table,
                        qualify_columns,
                        bound_params,
                        param_type_summary,
                    )
                })
                .collect();
            Ok(format!("({})", subs?.join(" OR ")))
        }
        Predicate::Not { child } => Ok(format!(
            "NOT ({})",
            emit_predicate(
                dialect,
                metadata,
                child,
                table,
                qualify_columns,
                bound_params,
                param_type_summary,
            )?
        )),
        Predicate::Filter { filter } => {
            let col = qualify_base_column(
                dialect,
                table,
                &metadata.fields[filter.field.index].column_name,
                qualify_columns,
            );
            let (clause, param) = filter_clause(
                dialect,
                &col,
                &filter.operator,
                bound_params.len() + 1,
                filter.value.clone(),
            );
            if let Some(value) = param {
                param_type_summary.push(format!("{}:{}", filter.field.name, filter.operator));
                bound_params.push(value);
            }
            Ok(clause)
        }
    }
}

/// Build a WHERE predicate and optional bound parameter for a filter.
///
/// `field_type` is used to dispatch type-specific operators (array `@>`,
/// JSONB `?`, etc.) that share a name like `"contains"` with text operators.
fn filter_clause(
    dialect: Dialect,
    col: &str,
    operator: &str,
    field_type: ferrum_core::ir::metadata::FieldType,
    param_index: usize,
    value: BindValue,
) -> (String, Option<BindValue>) {
    use ferrum_core::ir::metadata::FieldType;
    match operator {
        "is_null" => (format!("{col} IS NULL"), None),
        "is_not_null" => (format!("{col} IS NOT NULL"), None),
        "match" => {
            if dialect != Dialect::Postgres {
                let placeholder = dialect.placeholder(param_index);
                return (format!("{col} LIKE {placeholder}"), Some(value));
            }
            let placeholder = dialect.placeholder(param_index);
            (
                format!("{col} @@ plainto_tsquery({placeholder})"),
                Some(value),
            )
        }
        // Array containment operators.
        "contains"
            if matches!(
                field_type,
                FieldType::ArrayText
                    | FieldType::ArrayInt
                    | FieldType::ArrayUuid
                    | FieldType::ArrayFloat
            ) =>
        {
            let placeholder = dialect.placeholder(param_index);
            (format!("{col} @> {placeholder}"), Some(value))
        }
        "contained_by"
            if matches!(
                field_type,
                FieldType::ArrayText
                    | FieldType::ArrayInt
                    | FieldType::ArrayUuid
                    | FieldType::ArrayFloat
            ) =>
        {
            let placeholder = dialect.placeholder(param_index);
            (format!("{col} <@ {placeholder}"), Some(value))
        }
        "overlap"
            if matches!(
                field_type,
                FieldType::ArrayText
                    | FieldType::ArrayInt
                    | FieldType::ArrayUuid
                    | FieldType::ArrayFloat
            ) =>
        {
            let placeholder = dialect.placeholder(param_index);
            (format!("{col} && {placeholder}"), Some(value))
        }
        // JSONB operators.
        "contains" if field_type == FieldType::Json => {
            let placeholder = dialect.placeholder(param_index);
            (format!("{col} @> {placeholder}"), Some(value))
        }
        "has_key" if field_type == FieldType::Json => {
            let placeholder = dialect.placeholder(param_index);
            (format!("{col} ? {placeholder}"), Some(value))
        }
        "has_any_keys" if field_type == FieldType::Json => {
            let placeholder = dialect.placeholder(param_index);
            (format!("{col} ?| {placeholder}"), Some(value))
        }
        op => {
            let placeholder = dialect.placeholder(param_index);
            let sql_op = operator_to_sql(op, dialect);
            (format!("{col} {sql_op} {placeholder}"), Some(value))
        }
    }
}

fn postgres_value_cast(
    field_type: ferrum_core::ir::metadata::FieldType,
    placeholder: &str,
) -> String {
    use ferrum_core::ir::metadata::FieldType;
    let cast = match field_type {
        FieldType::Int | FieldType::BigInt => "bigint",
        FieldType::Float | FieldType::Decimal => "double precision",
        FieldType::Text | FieldType::Uuid | FieldType::TsVector => "text",
        FieldType::Bool => "boolean",
        FieldType::Datetime => "timestamptz",
        FieldType::Date => "date",
        FieldType::Time => "time",
        FieldType::Json => "jsonb",
        FieldType::Bytes => "bytea",
        FieldType::Vector => "vector",
    };
    format!("{placeholder}::{cast}")
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
fn operator_to_sql(op: &str, dialect: Dialect) -> &'static str {
    match op {
        "ne" => "<>",
        "gt" => ">",
        "gte" => ">=",
        "lt" => "<",
        "lte" => "<=",
        "is_null" => "IS NULL",
        "is_not_null" => "IS NOT NULL",
        "icontains" if dialect == Dialect::Postgres => "ILIKE",
        "icontains" | "contains" => "LIKE",
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
    use crate::dialect::Dialect;
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
            predicate: None,
            distinct: false,
            exists: false,
            joins: vec![],
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
        let q = emit_select(Dialect::Postgres, &meta, &ir).unwrap();
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
        let q = emit_select(Dialect::Postgres, &meta, &ir).unwrap();
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

        let q = emit_select(Dialect::Postgres, &meta, &ir).unwrap();
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
        let err = emit_select(Dialect::Postgres, &meta, &ir).unwrap_err();
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
        let err = emit_select(Dialect::Postgres, &meta, &ir).unwrap_err();
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
        let err = emit_select(Dialect::Postgres, &meta, &ir).unwrap_err();
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

        let q = emit_select(Dialect::Postgres, &meta, &ir).unwrap();

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
        let q1 = emit_select(Dialect::Postgres, &meta, &ir).unwrap();
        let q2 = emit_select(Dialect::Postgres, &meta, &ir).unwrap();
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
            predicate: None,
            distinct: false,
            exists: false,
            joins: vec![],
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
        let q = emit_insert(Dialect::Postgres, &meta, &ir).unwrap();

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
        let q = emit_insert(Dialect::Postgres, &meta, &ir).unwrap();
        // RETURNING must include both model fields.
        assert!(q.sql_text.contains("\"id\""));
        assert!(q.sql_text.contains("\"email\""));
    }

    #[test]
    fn emit_bulk_insert_multi_row_placeholders() {
        let meta = make_metadata();
        let ir = QuerySetIR {
            version: ferrum_core::ir::IR_VERSION,
            model_name: "User".into(),
            operation: ferrum_core::ir::Operation::BulkInsert {
                rows: vec![
                    vec![
                        (
                            FieldRef {
                                name: "id".into(),
                                index: 0,
                            },
                            BindValue::Int(1),
                        ),
                        (
                            FieldRef {
                                name: "email".into(),
                                index: 1,
                            },
                            BindValue::Text("a@example.com".into()),
                        ),
                    ],
                    vec![
                        (
                            FieldRef {
                                name: "id".into(),
                                index: 0,
                            },
                            BindValue::Int(2),
                        ),
                        (
                            FieldRef {
                                name: "email".into(),
                                index: 1,
                            },
                            BindValue::Text("b@example.com".into()),
                        ),
                    ],
                ],
                returning: true,
            },
            filters: vec![],
            order_by: vec![],
            limit: None,
            offset: None,
            vector_order_by: None,
            predicate: None,
            distinct: false,
            exists: false,
            joins: vec![],
        };
        let q = emit_bulk_insert(Dialect::Postgres, &meta, &ir).unwrap();
        assert!(q.sql_text.contains("VALUES ($1, $2), ($3, $4)"));
        assert!(!q.sql_text.contains("a@example.com"));
        assert_eq!(q.bound_params.len(), 4);
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
            predicate: None,
            distinct: false,
            exists: false,
            joins: vec![],
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
        let err = emit_update(Dialect::Postgres, &meta, &ir).unwrap_err();
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
        let q = emit_update(Dialect::Postgres, &meta, &ir).unwrap();
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
            predicate: None,
            distinct: false,
            exists: false,
            joins: vec![],
        }
    }

    /// Unfiltered DELETE must be rejected with `MissingFilter` — unscoped-mutation guard.
    #[test]
    fn emit_delete_rejects_empty_filters() {
        let meta = make_metadata();
        let ir = delete_ir(vec![]);
        let err = emit_delete(Dialect::Postgres, &meta, &ir).unwrap_err();
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
        let q = emit_delete(Dialect::Postgres, &meta, &ir).unwrap();
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
