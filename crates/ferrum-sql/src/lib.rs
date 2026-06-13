//! `ferrum-sql`: PostgreSQL-dialect SQL emission.
//!
//! Receives validated AST/IR nodes from `ferrum-core` and emits parameterized
//! PostgreSQL SQL text (`$1`, `$2`, … placeholders).
//!
//! # Security invariants
//! - Only produces parameterized SQL. Bound values NEVER appear in the SQL text.
//! - Identifiers (table names, column names) come exclusively from
//!   `ModelMetadata` allowlists, never from user input.
//! - This crate has no PyO3 dependency (enforced by CI `cargo tree` / `cargo-deny` check).

#![forbid(unsafe_code)]
#![warn(clippy::all, clippy::pedantic)]

pub mod dialect;
pub mod emit;
