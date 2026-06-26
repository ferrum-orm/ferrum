//! Schema diff → migration plan.
//!
//! Given current and target schema snapshots, produces a deterministic ordered
//! list of migration operations and a canonical plan digest.
//!
//! # Design notes
//! - The planner is pure: same inputs always produce the same plan + digest.
//! - The digest is used by the Python migration ledger for idempotency checks.
//! - ADR-004 (migration transactionality) governs which operations can run inside
//!   a transaction and which require special handling (e.g. `CREATE INDEX CONCURRENTLY`).

use crate::error::PlanError;

/// A single migration operation in the plan.
///
/// Each variant carries pre-computed SQL (produced by the Python planner) plus
/// the structural fields needed for dry-run display and idempotency checks.
/// Rust stores and serializes these verbatim — it never executes SQL.
#[derive(Debug, Clone)]
pub enum MigrationOp {
    /// `CREATE TABLE <table> (…)`.
    CreateTable { table: String, sql: String },
    /// `ALTER TABLE <table> ADD COLUMN <column> …`.
    AddColumn {
        table: String,
        column: String,
        sql: String,
    },
    /// `ALTER TABLE <table> DROP COLUMN <column>`. Destructive — requires confirmation.
    DropColumn {
        table: String,
        column: String,
        sql: String,
    },
    /// `ALTER TABLE <table> ALTER COLUMN <column> TYPE …`. May be destructive
    /// (type narrowing); Python planner sets `requires_confirmation` accordingly.
    AlterColumnType {
        table: String,
        column: String,
        sql: String,
    },
    /// `DROP TABLE <table>`. Destructive — requires confirmation.
    DropTable { table: String, sql: String },
    /// `CREATE [UNIQUE] INDEX [CONCURRENTLY] <index> ON <table> …`.
    ///
    /// `concurrent = true` means `CREATE INDEX CONCURRENTLY`, which must run
    /// outside a transaction (ADR-004). The Python apply path handles this by
    /// committing the surrounding transaction before executing the statement.
    CreateIndex {
        table: String,
        index: String,
        sql: String,
        /// When `true`, the statement must run outside a transaction (ADR-004).
        concurrent: bool,
    },
    /// Arbitrary SQL supplied by the Python planner (e.g. RLS policies, extensions).
    /// Must not contain credentials or bound values — safe for dry-run output.
    Raw { sql: String },
}

/// The output of a migration plan pass.
#[derive(Debug, Clone)]
pub struct MigrationPlan {
    /// Ordered list of operations to apply.
    pub operations: Vec<MigrationOp>,
    /// SHA-256 hex digest of the canonical plan representation.
    ///
    /// Stored in the Python migration ledger to detect out-of-band edits
    /// (see `PlanError::DigestMismatch`).
    pub digest: String,
}

/// Compute a migration plan from two schema snapshots.
///
/// **Placeholder implementation** — the real schema-diff planner will land in a
/// later wave. Currently returns an empty plan and empty digest without error.
/// The `PyO3` boundary stub (`plan_migration` in `ferrum-pyo3`) raises
/// `NotImplementedError` until this is implemented.
///
/// # Errors
/// Returns `PlanError` if the diff is ambiguous or the digest cannot be computed.
pub fn plan_migration(
    _current_schema: &str,
    _target_schema: &str,
) -> Result<MigrationPlan, PlanError> {
    // Placeholder — real implementation follows schema-diff landing.
    Ok(MigrationPlan {
        operations: Vec::new(),
        digest: String::new(),
    })
}
