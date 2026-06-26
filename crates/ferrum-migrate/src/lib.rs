//! Migration operation IR — pure data, no I/O, no async.
//! Python orchestrates apply; Rust only plans and serializes.
//!
//! # Security
//!
//! `MigrationPlan::to_json()` never includes connection strings, credentials,
//! or bound parameter values.  It is safe to emit as dry-run output.
//!
//! # Destructive / confirmation semantics
//!
//! `MigrationPlan::new` automatically computes `destructive` and
//! `requires_confirmation` from the op list so callers cannot forget to set
//! them.  Python must check `requires_confirmation` before calling apply.

use serde::{Deserialize, Serialize};

/// A single migration operation.
///
/// The enum is tagged with `"kind"` so round-tripped JSON is self-describing.
/// All identifier fields (table, column, index names) come from model-metadata
/// allowlists on the Python side before they reach this type; Rust stores and
/// serializes them verbatim.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum MigrationOp {
    /// `CREATE TABLE <table> (…)` with the given column definitions.
    CreateTable {
        table: String,
        columns: Vec<ColumnDef>,
    },
    /// `DROP TABLE <table>`. Destructive — `MigrationPlan::destructive` will be `true`.
    DropTable { table: String },
    /// `ALTER TABLE <table> ADD COLUMN …`.
    AddColumn { table: String, column: ColumnDef },
    /// `ALTER TABLE <table> DROP COLUMN <column>`. Destructive.
    DropColumn { table: String, column: String },
    /// `ALTER TABLE <table> RENAME COLUMN <from> TO <to>`. Non-destructive.
    RenameColumn {
        table: String,
        from: String,
        to: String,
    },
    /// `CREATE [UNIQUE] INDEX <name> ON <table> (<columns>)`.
    AddIndex {
        table: String,
        name: String,
        columns: Vec<String>,
        unique: bool,
    },
    /// `DROP INDEX <name>`.
    DropIndex { name: String },
    /// Arbitrary SQL supplied by the Python planner.
    ///
    /// `safe = false` means the SQL may be destructive or irreversible.
    /// Python's apply path must require `--unsafe` (or explicit confirmation)
    /// when `safe = false`. Never include credentials or bound values here —
    /// `to_json()` emits this field verbatim in dry-run output.
    RawSql { sql: String, safe: bool },
}

/// Column definition used in `CreateTable` and `AddColumn` ops.
///
/// All identifier and type fields come from model-metadata allowlists on the
/// Python side — never from raw user input. The `default` and `sql_type` strings
/// are validated Python-side before reaching this type.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct ColumnDef {
    /// Database column name.
    pub name: String,
    /// SQL type expression, e.g. `"TEXT"`, `"BIGINT"`, `"UUID DEFAULT gen_random_uuid()"`.
    pub sql_type: String,
    /// Whether the column accepts NULL.
    pub nullable: bool,
    /// Optional SQL DEFAULT expression (Python-side default, not user input).
    pub default: Option<String>,
    /// Whether this column is part of the primary key.
    pub primary_key: bool,
}

/// An ordered sequence of migration operations with pre-computed safety flags.
///
/// Produced by the Python planner and handed to Rust for serialization
/// (dry-run) or stored verbatim for later apply.  Rust never executes SQL.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct MigrationPlan {
    pub version: u32,
    pub name: String,
    pub ops: Vec<MigrationOp>,
    /// `true` if any op is `DropTable` or `DropColumn`.
    pub destructive: bool,
    /// `true` if `destructive` or any `RawSql { safe: false }` op is present.
    pub requires_confirmation: bool,
}

impl MigrationPlan {
    /// Build a plan and auto-compute `destructive` / `requires_confirmation`.
    pub fn new(version: u32, name: impl Into<String>, ops: Vec<MigrationOp>) -> Self {
        let destructive = ops.iter().any(|op| {
            matches!(
                op,
                MigrationOp::DropTable { .. } | MigrationOp::DropColumn { .. }
            )
        });
        let requires_confirmation = destructive
            || ops
                .iter()
                .any(|op| matches!(op, MigrationOp::RawSql { safe: false, .. }));
        Self {
            version,
            name: name.into(),
            ops,
            destructive,
            requires_confirmation,
        }
    }

    /// Serialize to pretty-printed JSON for dry-run output or Python
    /// orchestration.  Never includes connection strings or bound values.
    ///
    /// # Errors
    ///
    /// Returns `serde_json::Error` if serialization fails (in practice only
    /// possible if the in-memory IR contains non-finite floats, which this
    /// type cannot).
    pub fn to_json(&self) -> Result<String, serde_json::Error> {
        serde_json::to_string_pretty(self)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sample_col(name: &str) -> ColumnDef {
        ColumnDef {
            name: name.to_owned(),
            sql_type: "TEXT".to_owned(),
            nullable: false,
            default: None,
            primary_key: false,
        }
    }

    #[test]
    fn test_create_table_plan_not_destructive() {
        let plan = MigrationPlan::new(
            1,
            "create_users",
            vec![MigrationOp::CreateTable {
                table: "users".to_owned(),
                columns: vec![sample_col("id"), sample_col("email")],
            }],
        );
        assert!(!plan.destructive);
        assert!(!plan.requires_confirmation);
    }

    #[test]
    fn test_drop_table_plan_is_destructive() {
        let plan = MigrationPlan::new(
            2,
            "drop_legacy",
            vec![MigrationOp::DropTable {
                table: "legacy".to_owned(),
            }],
        );
        assert!(plan.destructive);
        assert!(plan.requires_confirmation);
    }

    #[test]
    fn test_raw_sql_unsafe_requires_confirmation() {
        let plan = MigrationPlan::new(
            3,
            "raw_unsafe",
            vec![MigrationOp::RawSql {
                sql: "ALTER TABLE users ALTER COLUMN score TYPE FLOAT".to_owned(),
                safe: false,
            }],
        );
        assert!(!plan.destructive, "raw_sql is not considered 'destructive'");
        assert!(
            plan.requires_confirmation,
            "unsafe raw SQL must require confirmation"
        );
    }

    #[test]
    fn test_raw_sql_safe_no_confirmation() {
        let plan = MigrationPlan::new(
            4,
            "raw_safe",
            vec![MigrationOp::RawSql {
                sql: "CREATE INDEX CONCURRENTLY idx_users_email ON users (email)".to_owned(),
                safe: true,
            }],
        );
        assert!(!plan.destructive);
        assert!(!plan.requires_confirmation);
    }

    #[test]
    fn test_plan_serializes_to_json() {
        let original = MigrationPlan::new(
            5,
            "round_trip",
            vec![
                MigrationOp::CreateTable {
                    table: "posts".to_owned(),
                    columns: vec![ColumnDef {
                        name: "id".to_owned(),
                        sql_type: "BIGSERIAL".to_owned(),
                        nullable: false,
                        default: None,
                        primary_key: true,
                    }],
                },
                MigrationOp::AddIndex {
                    table: "posts".to_owned(),
                    name: "idx_posts_id".to_owned(),
                    columns: vec!["id".to_owned()],
                    unique: true,
                },
            ],
        );

        let json = original.to_json().expect("serialization must succeed");
        let recovered: MigrationPlan =
            serde_json::from_str(&json).expect("deserialization must succeed");

        assert_eq!(original.version, recovered.version);
        assert_eq!(original.name, recovered.name);
        assert_eq!(original.ops, recovered.ops);
        assert_eq!(original.destructive, recovered.destructive);
        assert_eq!(
            original.requires_confirmation,
            recovered.requires_confirmation
        );

        // Sanity: JSON must not contain any credentials or bound values.
        assert!(!json.contains("password"));
        assert!(!json.contains("dsn"));
    }

    #[test]
    fn test_column_def_nullable_default() {
        let col = ColumnDef {
            name: "score".to_owned(),
            sql_type: "INTEGER".to_owned(),
            nullable: true,
            default: Some("0".to_owned()),
            primary_key: false,
        };
        assert!(col.nullable);
        assert_eq!(col.default.as_deref(), Some("0"));
        assert!(!col.primary_key);

        let json = serde_json::to_string(&col).unwrap();
        let recovered: ColumnDef = serde_json::from_str(&json).unwrap();
        assert_eq!(col, recovered);
    }

    #[test]
    fn test_drop_column_plan_is_destructive() {
        let plan = MigrationPlan::new(
            6,
            "drop_col",
            vec![MigrationOp::DropColumn {
                table: "users".to_owned(),
                column: "old_field".to_owned(),
            }],
        );
        assert!(plan.destructive);
        assert!(plan.requires_confirmation);
    }

    #[test]
    fn test_rename_column_not_destructive() {
        let plan = MigrationPlan::new(
            7,
            "rename_col",
            vec![MigrationOp::RenameColumn {
                table: "users".to_owned(),
                from: "fname".to_owned(),
                to: "first_name".to_owned(),
            }],
        );
        assert!(!plan.destructive);
        assert!(!plan.requires_confirmation);
    }

    #[test]
    fn test_migration_op_serde_tag() {
        let op = MigrationOp::DropIndex {
            name: "idx_old".to_owned(),
        };
        let json = serde_json::to_string(&op).unwrap();
        assert!(
            json.contains(r#""kind":"drop_index""#),
            "kind tag must be snake_case: {json}"
        );
    }
}
