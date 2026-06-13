//! `ferrum-core`: pure, synchronous, stateless SQL compiler and row codec.
//!
//! # Invariants
//! - No `PyO3`, no Tokio, no network I/O.
//! - No `unsafe` (enforced by `#![forbid(unsafe_code)]`).
//! - Every public function is a pure transformation over borrowed or owned data.
//! - Compilation is a function `(&ModelMetadata, QuerySetIR) -> Result<CompiledQuery, CompileError>`.
//! - Hydration is a function `(&ModelMetadata, RawRows) -> Result<RowPayload, HydrateError>`.

#![forbid(unsafe_code)]
#![warn(clippy::all, clippy::pedantic)]

pub mod compile;
pub mod error;
pub mod hydrate;
pub mod ir;
pub mod migrate;
