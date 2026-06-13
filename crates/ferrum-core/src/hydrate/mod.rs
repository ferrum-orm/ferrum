//! Raw row data → typed `RowPayload` hydration.
//!
//! The hydrator validates DB-origin rows against model metadata allowlists and
//! returns column-keyed payloads ready for Python's construct-without-revalidate
//! fast path (ADR-003).
//!
//! # Trust model (ADR-003)
//! - Rows come from the DB driver (asyncpg), not from user input.
//! - Column names are resolved against `ModelMetadata::fields` allowlists.
//! - No user-supplied data reaches identifier positions here.
//! - The Pydantic `construct()` fast path on the Python side skips re-validation
//!   because the DB already enforced the schema. Custom validators with
//!   side-effects do NOT re-run on DB-origin data by default — document this to
//!   callers and offer opt-in full validation if needed.

use crate::{error::HydrateError, ir::ModelMetadata};
use serde_json::{Map, Value};

/// A hydrated row ready for `model_construct(**payload)` on the Python side.
pub type RowPayload = Map<String, Value>;

/// Hydrate a batch of DB-origin rows into typed payloads.
///
/// Each row is a column-name → `serde_json::Value` map produced by the `PyO3`
/// bridge from asyncpg records. This function:
/// 1. Validates that every required (non-nullable) column is present and non-null.
/// 2. Returns the rows unchanged as `RowPayload` values — type coercion is
///    Pydantic's responsibility on the Python side (ADR-003).
///
/// # Errors
/// Returns `HydrateError::MissingColumn` if a required (non-nullable) field is
/// absent or null in any row.
pub fn hydrate_rows(
    metadata: &ModelMetadata,
    rows: Vec<RowPayload>,
) -> Result<Vec<RowPayload>, HydrateError> {
    let mut result = Vec::with_capacity(rows.len());
    for row in rows {
        validate_row(metadata, &row)?;
        result.push(row);
    }
    Ok(result)
}

/// Check that all required (non-nullable) columns are present and non-null.
///
/// Two distinct error variants allow callers to triage root cause:
/// - `MissingColumn`: the column was not included in the result projection at all.
/// - `NullNonNullable`: the column is present but carries a NULL value, indicating
///   a DB schema constraint violation (non-nullable column returned NULL).
fn validate_row(metadata: &ModelMetadata, row: &RowPayload) -> Result<(), HydrateError> {
    for field in &metadata.fields {
        if !field.nullable {
            match row.get(&field.column_name) {
                None => {
                    return Err(HydrateError::MissingColumn {
                        model: metadata.model_name.clone(),
                        column: field.column_name.clone(),
                    });
                }
                Some(Value::Null) => {
                    return Err(HydrateError::NullNonNullable {
                        model: metadata.model_name.clone(),
                        column: field.column_name.clone(),
                    });
                }
                Some(_) => {}
            }
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ir::metadata::{FieldMeta, FieldType};
    use serde_json::json;

    fn make_metadata() -> ModelMetadata {
        ModelMetadata {
            model_name: "Post".into(),
            table_name: "posts".into(),
            fields: vec![
                FieldMeta {
                    name: "id".into(),
                    column_name: "id".into(),
                    field_type: FieldType::Int,
                    allowed_operators: vec!["eq".into()],
                    nullable: false,
                },
                FieldMeta {
                    name: "title".into(),
                    column_name: "title".into(),
                    field_type: FieldType::Text,
                    allowed_operators: vec!["eq".into(), "icontains".into()],
                    nullable: false,
                },
                FieldMeta {
                    name: "bio".into(),
                    column_name: "bio".into(),
                    field_type: FieldType::Text,
                    allowed_operators: vec!["eq".into()],
                    nullable: true, // nullable column
                },
            ],
            pk_index: 0,
        }
    }

    #[test]
    fn hydrates_valid_rows() {
        let meta = make_metadata();
        let rows = vec![
            serde_json::from_value(json!({"id": 1, "title": "Hello", "bio": null})).unwrap(),
            serde_json::from_value(json!({"id": 2, "title": "World", "bio": "bio text"})).unwrap(),
        ];
        let result = hydrate_rows(&meta, rows).unwrap();
        assert_eq!(result.len(), 2);
        assert_eq!(result[0]["id"], json!(1));
        assert_eq!(result[1]["title"], json!("World"));
    }

    #[test]
    fn rejects_missing_required_column() {
        let meta = make_metadata();
        // Row is missing the required "title" column.
        let rows = vec![serde_json::from_value(json!({"id": 1, "bio": "some bio"})).unwrap()];
        let err = hydrate_rows(&meta, rows).unwrap_err();
        assert!(
            matches!(err, HydrateError::MissingColumn { ref column, .. } if column == "title"),
            "expected MissingColumn for 'title', got {err:?}"
        );
    }

    #[test]
    fn rejects_null_in_required_column() {
        let meta = make_metadata();
        // "title" is non-nullable but the row has it as null — NullNonNullable, not MissingColumn.
        let rows =
            vec![serde_json::from_value(json!({"id": 1, "title": null, "bio": null})).unwrap()];
        let err = hydrate_rows(&meta, rows).unwrap_err();
        assert!(
            matches!(err, HydrateError::NullNonNullable { ref column, .. } if column == "title"),
            "expected NullNonNullable for 'title', got {err:?}"
        );
    }

    #[test]
    fn allows_null_in_nullable_column() {
        let meta = make_metadata();
        // "bio" is nullable; null should be accepted.
        let rows =
            vec![serde_json::from_value(json!({"id": 1, "title": "Hello", "bio": null})).unwrap()];
        assert!(hydrate_rows(&meta, rows).is_ok());
    }

    /// Nullable field with NULL → `Value::Null` passes through to the payload.
    #[test]
    fn hydrate_null_nullable_field_ok() {
        let meta = make_metadata();
        let rows =
            vec![serde_json::from_value(json!({"id": 1, "title": "Hello", "bio": null})).unwrap()];
        let result = hydrate_rows(&meta, rows).unwrap();
        assert_eq!(result[0]["bio"], serde_json::Value::Null);
    }

    /// Non-nullable field with NULL in DB row → `HydrateError::NullNonNullable`.
    #[test]
    fn hydrate_null_nonnullable_raises_error() {
        let meta = make_metadata();
        let rows =
            vec![serde_json::from_value(json!({"id": 1, "title": null, "bio": "text"})).unwrap()];
        let err = hydrate_rows(&meta, rows).unwrap_err();
        assert!(
            matches!(err, HydrateError::NullNonNullable { ref column, .. } if column == "title"),
            "expected NullNonNullable for 'title', got {err:?}"
        );
    }

    #[test]
    fn empty_input_returns_empty_output() {
        let meta = make_metadata();
        let result = hydrate_rows(&meta, vec![]).unwrap();
        assert!(result.is_empty());
    }
}
