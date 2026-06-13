//! Raw row bytes → typed `RowPayload` hydration.
//!
//! The hydrator decodes DB-origin rows using model metadata allowlists.
//! It operates on trusted data (the DB already enforced types); the Pydantic
//! construct-without-revalidate fast path is used on the Python side (ADR-003).
//!
//! # Trust model
//! - Rows come from the DB driver (asyncpg), not from user input.
//! - Column names are resolved against `ModelMetadata::fields` allowlists.
//! - No user-supplied data reaches identifier positions here.

use crate::{error::HydrateError, ir::ModelMetadata};
use std::collections::HashMap;
use std::hash::BuildHasher;

/// A single hydrated row: column name → decoded value.
pub type RowPayload = HashMap<String, HydratedValue>;

/// A decoded column value.
#[derive(Debug, Clone)]
pub enum HydratedValue {
    Null,
    Bool(bool),
    Int(i64),
    Float(f64),
    Text(String),
    Bytes(Vec<u8>),
}

/// Placeholder: hydrate a raw row (represented as a map of column → raw bytes)
/// into a `RowPayload`. The real implementation will decode asyncpg wire bytes.
///
/// # Errors
/// Returns `HydrateError` if a required column is absent or a type does not match.
pub fn hydrate_row<S: BuildHasher>(
    _metadata: &ModelMetadata,
    _raw: &HashMap<String, Vec<u8>, S>,
) -> Result<RowPayload, HydrateError> {
    // Placeholder — real implementation wired when asyncpg integration lands.
    Ok(HashMap::new())
}
