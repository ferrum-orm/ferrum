//! `ferrum-pyo3`: thin `PyO3` extension bridge.
//!
//! This is the ONLY crate in the workspace that may declare a `pyo3` dependency.
//! CI enforces this via `cargo tree` / `cargo-deny` checks (`PROJECT_STRUCTURE.md` §6.4).
//!
//! # Boundary contract
//! - All public functions call into `ferrum-core` / `ferrum-sql` synchronously while
//!   holding the GIL. Compilation is sub-millisecond CPU-bound work; do NOT release
//!   the GIL or introduce async Rust here.
//! - `Result::Err` from core/sql → catchable Python exception (never process abort).
//! - Rust panics are caught by `std::panic::catch_unwind` inside each `#[pyfunction]`
//!   and surfaced as `FerrumInternalError` (ARCHITECTURE.md §6.2, ERR-2).
//! - Error payloads carry structured fields only — no trace blobs, no raw `PostgreSQL`
//!   DETAIL/HINT, no memory addresses, no local paths.

// PyO3 0.22.x uses an internal `cfg(gil-refs)` feature gate that Rust's
// `unexpected_cfgs` lint flags. This is a known upstream issue resolved in
// PyO3 0.23+. Suppress here until the pyo3 dependency is upgraded.
#![allow(unexpected_cfgs)]
// PyO3 0.22's `#[pyfunction]` macro expands code that triggers `useless_conversion`
// (false positive: macro-generated `PyErr → PyErr` coercions). Remove on pyo3 >= 0.23.
#![allow(clippy::useless_conversion)]

use pyo3::exceptions::{PyNotImplementedError, PyRuntimeError};
use pyo3::prelude::*;

// With abi3, PyO3 does not support subclassing native types via `extends`.
// Use `create_exception!` to define custom exception classes that inherit from
// `RuntimeError` on the Python side. These are registered into the module by
// `m.add(name, exc)` rather than `m.add_class::<T>()`.
pyo3::create_exception!(ferrum_native, FerrumInternalError, PyRuntimeError);
pyo3::create_exception!(ferrum_native, FerrumCompileError, PyRuntimeError);
pyo3::create_exception!(ferrum_native, FerrumHydrationError, PyRuntimeError);

/// Compile a `QuerySetIR` (JSON-serialized) against model metadata (JSON-serialized).
///
/// Returns a dict with keys `sql_text`, `bound_params`, `param_type_summary`,
/// `fingerprint`, and `operation` (`"select"` / `"insert"` / `"update"` / `"delete"`).
/// Python uses `operation` to route the compiled SQL to the correct asyncpg call
/// (`fetch` for select/insert/update, `execute` for delete or update-without-return).
///
/// # Errors
/// - `FerrumCompileError` — IR invalid: unknown field, unsupported operator, version
///   mismatch, missing filter on mutation, or malformed JSON.
/// - `FerrumInternalError` — Rust panic (should never occur in normal use; ERR-2).
#[pyfunction]
fn compile_query(
    py: Python<'_>,
    metadata_json: &str,
    ir_json: &str,
    dialect: &str,
) -> PyResult<Py<PyAny>> {
    // `AssertUnwindSafe` is sound here: we only share `&str` (Copy, RefUnwindSafe)
    // across the panic boundary, and the closure is called exactly once.
    let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        let metadata: ferrum_core::ir::ModelMetadata = serde_json::from_str(metadata_json)
            .map_err(|e| ferrum_core::error::CompileError::MalformedIr {
                reason: format!("metadata deserialization failed: {e}"),
            })?;
        let ir: ferrum_core::ir::QuerySetIR = serde_json::from_str(ir_json).map_err(|e| {
            ferrum_core::error::CompileError::MalformedIr {
                reason: format!("IR deserialization failed: {e}"),
            }
        })?;

        let sql_dialect = ferrum_sql::dialect::Dialect::parse(dialect).ok_or_else(|| {
            ferrum_core::error::CompileError::MalformedIr {
                reason: format!("unknown dialect {dialect:?}; expected postgres, mysql, or sqlite"),
            }
        })?;

        // Determine operation name for the Python routing key.
        let operation_name: &'static str = match &ir.operation {
            ferrum_core::ir::Operation::Select { .. } => "select",
            ferrum_core::ir::Operation::Insert { .. } => "insert",
            ferrum_core::ir::Operation::Update { .. } => "update",
            ferrum_core::ir::Operation::Delete { .. } => "delete",
        };

        // Dispatch to the correct emitter. Each emitter calls
        // `ferrum_core::compile::compile` first — allowlist checks fail fast
        // before any SQL text is produced (SQL-1).
        let compiled = match &ir.operation {
            ferrum_core::ir::Operation::Select { .. } => {
                ferrum_sql::emit::emit_select(sql_dialect, &metadata, &ir)
            }
            ferrum_core::ir::Operation::Insert { .. } => {
                ferrum_sql::emit::emit_insert(sql_dialect, &metadata, &ir)
            }
            ferrum_core::ir::Operation::Update { .. } => {
                ferrum_sql::emit::emit_update(sql_dialect, &metadata, &ir)
            }
            ferrum_core::ir::Operation::Delete { .. } => {
                ferrum_sql::emit::emit_delete(sql_dialect, &metadata, &ir)
            }
        }?;

        Ok::<_, ferrum_core::error::CompileError>((compiled, operation_name))
    }));

    match result {
        Ok(Ok((compiled, operation_name))) => {
            let dict = pyo3::types::PyDict::new_bound(py);
            dict.set_item("sql_text", &compiled.sql_text)?;
            // `bound_params` are JSON-encoded so Python can deserialize them into
            // native types for the asyncpg driver. Never log these in Tier A hooks.
            let params: Vec<String> = compiled
                .bound_params
                .iter()
                .map(|v| serde_json::to_string(v).unwrap_or_default())
                .collect();
            dict.set_item("bound_params", params)?;
            dict.set_item("param_type_summary", compiled.param_type_summary)?;
            dict.set_item("fingerprint", &compiled.fingerprint)?;
            // Python routes on this: "select"/"insert"/"update"/"delete".
            dict.set_item("operation", operation_name)?;
            Ok(dict.into())
        }
        Ok(Err(compile_err)) => {
            // Structured compile error → catchable Python exception.
            // The message contains model/field/operator names — no user values.
            Err(FerrumCompileError::new_err(format!("{compile_err}")))
        }
        Err(_panic_payload) => {
            // Rust panic → sanitized FerrumInternalError; no address/path leak.
            Err(FerrumInternalError::new_err(
                "internal Ferrum error: unexpected panic in Rust core (category: compile)",
            ))
        }
    }
}

/// Convert a `serde_json::Value` to a Python object.
///
/// Mapping: `null` → `None`, `bool` → `bool`, `number` → `int` or `float`,
/// `string` → `str`, `array` → `list`, `object` → `dict`.
fn json_value_to_pyobj(py: Python<'_>, val: &serde_json::Value) -> PyResult<PyObject> {
    match val {
        serde_json::Value::Null => Ok(py.None()),
        serde_json::Value::Bool(b) => Ok(b.into_py(py)),
        serde_json::Value::Number(n) => {
            if let Some(i) = n.as_i64() {
                Ok(i.into_py(py))
            } else if let Some(f) = n.as_f64() {
                Ok(f.into_py(py))
            } else {
                // u64 values > i64::MAX are represented as u64 in serde_json.
                // Try widening to u64 before giving up.
                if let Some(u) = n.as_u64() {
                    Ok(u.into_py(py))
                } else {
                    Err(FerrumHydrationError::new_err(
                        "numeric value is out of representable range",
                    ))
                }
            }
        }
        serde_json::Value::String(s) => Ok(s.as_str().into_py(py)),
        serde_json::Value::Array(arr) => {
            let list = pyo3::types::PyList::empty_bound(py);
            for item in arr {
                list.append(json_value_to_pyobj(py, item)?)?;
            }
            Ok(list.into())
        }
        serde_json::Value::Object(obj) => {
            let dict = pyo3::types::PyDict::new_bound(py);
            for (k, v) in obj {
                dict.set_item(k, json_value_to_pyobj(py, v)?)?;
            }
            Ok(dict.into())
        }
    }
}

/// Hydrate a batch of DB-origin rows against model metadata.
///
/// `rows_json` must be a JSON array of objects mapping column names to values.
/// The hydrator validates that all required (non-nullable) columns are present
/// and delegates type coercion to Pydantic on the Python side (ADR-003 fast path).
///
/// # Errors
/// - `FerrumHydrationError` — metadata or rows deserialization failed, or a required
///   column is missing/null in a row.
/// - `FerrumInternalError` — Rust panic (should never occur in normal use; ERR-2).
#[pyfunction]
fn hydrate_rows(py: Python<'_>, metadata_json: &str, rows_json: &str) -> PyResult<PyObject> {
    // `AssertUnwindSafe` is sound: only `&str` (Copy, RefUnwindSafe) crosses the
    // panic boundary, called exactly once.
    let unwind_result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        let metadata: ferrum_core::ir::ModelMetadata = serde_json::from_str(metadata_json)
            .map_err(|e| format!("metadata deserialization error: {e}"))?;
        // `Vec<Map<String, Value>>` deserializes directly from a JSON array of objects.
        let rows: Vec<ferrum_core::hydrate::RowPayload> = serde_json::from_str(rows_json)
            .map_err(|e| format!("rows deserialization error: {e}"))?;
        ferrum_core::hydrate::hydrate_rows(&metadata, rows).map_err(|e| format!("{e}"))
    }));

    match unwind_result {
        Ok(Ok(rows)) => {
            let list = pyo3::types::PyList::empty_bound(py);
            for row in &rows {
                let dict = pyo3::types::PyDict::new_bound(py);
                for (k, v) in row {
                    dict.set_item(k, json_value_to_pyobj(py, v)?)?;
                }
                list.append(dict)?;
            }
            Ok(list.into())
        }
        Ok(Err(msg)) => Err(FerrumHydrationError::new_err(msg)),
        Err(_panic_payload) => Err(FerrumInternalError::new_err(
            "internal Ferrum error: unexpected panic in Rust core (category: hydrate)",
        )),
    }
}

/// Migration planning stub — not yet implemented (Wave 4).
///
/// # Errors
/// Always raises `NotImplementedError`.
#[pyfunction]
fn plan_migration() -> PyResult<()> {
    Err(PyNotImplementedError::new_err(
        "Migration planning is not yet implemented",
    ))
}

/// The Python extension module `ferrum._native`.
///
/// The function must be named `_native` to match the final component of
/// `module-name = "ferrum._native"` in `pyproject.toml`. PyO3 generates the
/// `PyInit__native` C symbol from this function name.
#[pymodule]
fn _native(py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(compile_query, m)?)?;
    m.add_function(wrap_pyfunction!(hydrate_rows, m)?)?;
    m.add_function(wrap_pyfunction!(plan_migration, m)?)?;
    m.add(
        "FerrumInternalError",
        py.get_type_bound::<FerrumInternalError>(),
    )?;
    m.add(
        "FerrumCompileError",
        py.get_type_bound::<FerrumCompileError>(),
    )?;
    m.add(
        "FerrumHydrationError",
        py.get_type_bound::<FerrumHydrationError>(),
    )?;
    Ok(())
}
