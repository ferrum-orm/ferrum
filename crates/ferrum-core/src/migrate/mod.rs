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
#[derive(Debug, Clone)]
pub enum MigrationOp {
    CreateTable {
        table: String,
        sql: String,
    },
    AddColumn {
        table: String,
        column: String,
        sql: String,
    },
    DropColumn {
        table: String,
        column: String,
        sql: String,
    },
    AlterColumnType {
        table: String,
        column: String,
        sql: String,
    },
    DropTable {
        table: String,
        sql: String,
    },
    CreateIndex {
        table: String,
        index: String,
        sql: String,
        concurrent: bool,
    },
    Raw {
        sql: String,
    },
}

/// The output of a migration plan pass.
#[derive(Debug, Clone)]
pub struct MigrationPlan {
    pub operations: Vec<MigrationOp>,
    /// SHA-256 hex digest of the canonical plan representation.
    pub digest: String,
}

/// Placeholder: compute a migration plan from two schema snapshots.
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
