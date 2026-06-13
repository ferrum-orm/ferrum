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
//! - Rust panics are caught by the `#[pyfunction]` wrapper and surfaced as
//!   `FerrumInternalError` (ARCHITECTURE.md §6.2, ERR-2).
//! - Error payloads carry structured fields only — no trace blobs, no raw `PostgreSQL`
//!   DETAIL/HINT, no memory addresses, no local paths.

// PyO3 0.22.x uses an internal `cfg(gil-refs)` feature gate that Rust's
// `unexpected_cfgs` lint flags. This is a known upstream issue resolved in
// PyO3 0.23+. Suppress here until the pyo3 dependency is upgraded.
#![allow(unexpected_cfgs)]
// PyO3 0.22's `#[pyfunction]` macro expands code that triggers `useless_conversion`
// (false positive: macro-generated `PyErr → PyErr` coercions). Remove on pyo3 >= 0.23.
#![allow(clippy::useless_conversion)]

use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;

// With abi3, PyO3 does not support subclassing native types via `extends`.
// Use `create_exception!` to define custom exception classes that inherit from
// `RuntimeError` on the Python side. These are registered into the module by
// `m.add(name, exc)` rather than `m.add_class::<T>()`.
pyo3::create_exception!(ferrum_native, FerrumInternalError, PyRuntimeError);
pyo3::create_exception!(ferrum_native, FerrumCompileError, PyRuntimeError);

/// Compile a `QuerySetIR` (JSON-serialized) against model metadata (JSON-serialized).
///
/// Returns a dict with keys `sql_text`, `bound_params`, `param_type_summary`.
///
/// Raises:
///   `FerrumCompileError` — if IR is invalid (unknown field, bad operator, version mismatch).
///   `FerrumInternalError` — if a Rust panic occurs (should never happen in normal use).
#[pyfunction]
fn compile_query(py: Python<'_>, metadata_json: &str, ir_json: &str) -> PyResult<Py<PyAny>> {
    // Deserialize inputs — JSON is the interim serialization until ADR-002 finalizes
    // the binary IR contract.
    let metadata: ferrum_core::ir::ModelMetadata = serde_json::from_str(metadata_json)
        .map_err(|e| PyRuntimeError::new_err(format!("metadata deserialization error: {e}")))?;

    let ir: ferrum_core::ir::QuerySetIR = serde_json::from_str(ir_json)
        .map_err(|e| PyRuntimeError::new_err(format!("IR deserialization error: {e}")))?;

    // Run compilation — synchronous, GIL held (sub-millisecond, CPU-bound).
    let result = std::panic::catch_unwind(|| ferrum_sql::emit::emit_select(&metadata, &ir));

    match result {
        Ok(Ok(compiled)) => {
            let dict = pyo3::types::PyDict::new_bound(py);
            dict.set_item("sql_text", &compiled.sql_text)?;
            let params: Vec<String> = compiled
                .bound_params
                .iter()
                .map(|v| serde_json::to_string(v).unwrap_or_default())
                .collect();
            dict.set_item("bound_params", params)?;
            dict.set_item("param_type_summary", compiled.param_type_summary)?;
            Ok(dict.into())
        }
        Ok(Err(compile_err)) => {
            // Structured compile error → catchable Python exception.
            // The error message contains model/field/operator names — no user values.
            Err(FerrumCompileError::new_err(format!(
                "compile error: {compile_err}"
            )))
        }
        Err(_panic_payload) => {
            // Rust panic → sanitized FerrumInternalError; no address/path leak.
            Err(FerrumInternalError::new_err(
                "internal Ferrum error: unexpected panic in Rust core (category: compile)",
            ))
        }
    }
}

/// The Python extension module `ferrum._native`.
#[pymodule]
fn ferrum_native(py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(compile_query, m)?)?;
    m.add(
        "FerrumInternalError",
        py.get_type_bound::<FerrumInternalError>(),
    )?;
    m.add(
        "FerrumCompileError",
        py.get_type_bound::<FerrumCompileError>(),
    )?;
    Ok(())
}
