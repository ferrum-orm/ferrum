//! SQL dialect configuration: placeholder style, identifier quoting, RETURNING support.
//!
//! `Dialect` is the only mechanism by which the emitter varies SQL syntax.
//! It is never constructed from user-supplied strings at runtime — the `PyO3`
//! boundary calls `Dialect::parse` on a validated driver identifier and returns
//! a compile error for unrecognized values.

/// Supported SQL dialects for Ferrum's parameterized emitter.
///
/// Each variant controls three things: the positional parameter placeholder style
/// (`$N` vs `?`), the identifier quoting character (`"…"` / `` `…` `` / `[…]`),
/// and whether the dialect supports `RETURNING` or `OUTPUT INSERTED.*` for
/// surfacing mutated rows after INSERT/UPDATE.
///
/// `Postgres` is the canonical, fully-supported dialect. `Mysql`, `Sqlite`, and
/// `Mssql` are secondary — some operations (e.g. `BulkUpdate`) are
/// `Postgres`-only and return `CompileError::MalformedIr` for other dialects.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Dialect {
    /// `PostgreSQL` / asyncpg — positional `$N` placeholders, `"…"` quoting, `RETURNING`.
    Postgres,
    /// `MySQL` / `MariaDB` — `?` placeholders, `` `…` `` quoting, no `RETURNING`.
    Mysql,
    /// `SQLite` — `?` placeholders, `"…"` quoting, `RETURNING` (`SQLite` ≥ 3.35).
    Sqlite,
    /// Microsoft SQL Server (T-SQL) — `?` placeholders, `[…]` quoting, `OUTPUT INSERTED.*`.
    Mssql,
}

impl Dialect {
    /// Parse a dialect name from the `PyO3` boundary
    /// (`"postgres"`, `"mysql"`, `"sqlite"`, `"mssql"`).
    ///
    /// # Errors
    /// Returns `None` for unknown dialect strings.
    #[must_use]
    pub fn parse(s: &str) -> Option<Self> {
        match s.to_ascii_lowercase().as_str() {
            "postgres" | "postgresql" => Some(Self::Postgres),
            "mysql" | "mariadb" => Some(Self::Mysql),
            "sqlite" => Some(Self::Sqlite),
            "mssql" | "sqlserver" => Some(Self::Mssql),
            _ => None,
        }
    }

    /// Render a positional parameter placeholder for this dialect.
    #[must_use]
    pub fn placeholder(&self, position: usize) -> String {
        match self {
            Self::Postgres => format!("${position}"),
            Self::Mysql | Self::Sqlite | Self::Mssql => "?".to_string(),
        }
    }

    /// Quote an identifier (table or column name).
    ///
    /// Identifiers come exclusively from model metadata allowlists — this is a
    /// defense-in-depth measure, not the primary guard.
    #[must_use]
    pub fn quote_ident(&self, name: &str) -> String {
        match self {
            Self::Postgres | Self::Sqlite => format!("\"{}\"", name.replace('"', "\"\"")),
            Self::Mysql => format!("`{}`", name.replace('`', "``")),
            // T-SQL bracket quoting: escape a literal `]` by doubling it.
            Self::Mssql => format!("[{}]", name.replace(']', "]]")),
        }
    }

    /// Whether this dialect supports a trailing `RETURNING` clause on INSERT/UPDATE.
    #[must_use]
    pub fn supports_returning(&self) -> bool {
        matches!(self, Self::Postgres | Self::Sqlite)
    }

    /// Whether this dialect returns mutated rows via a T-SQL `OUTPUT INSERTED.*`
    /// clause instead of a trailing `RETURNING` clause.
    #[must_use]
    pub fn uses_output_returning(&self) -> bool {
        matches!(self, Self::Mssql)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_dialect_names() {
        assert_eq!(Dialect::parse("postgres"), Some(Dialect::Postgres));
        assert_eq!(Dialect::parse("mysql"), Some(Dialect::Mysql));
        assert_eq!(Dialect::parse("sqlite"), Some(Dialect::Sqlite));
        assert_eq!(Dialect::parse("mssql"), Some(Dialect::Mssql));
        assert_eq!(Dialect::parse("sqlserver"), Some(Dialect::Mssql));
        assert_eq!(Dialect::parse("SQLServer"), Some(Dialect::Mssql));
        assert!(Dialect::parse("oracle").is_none());
    }

    #[test]
    fn placeholder_format() {
        assert_eq!(Dialect::Postgres.placeholder(1), "$1");
        assert_eq!(Dialect::Mysql.placeholder(1), "?");
        assert_eq!(Dialect::Sqlite.placeholder(42), "?");
        assert_eq!(Dialect::Mssql.placeholder(1), "?");
    }

    #[test]
    fn quote_ident_postgres() {
        assert_eq!(Dialect::Postgres.quote_ident("users"), "\"users\"");
        assert_eq!(
            Dialect::Postgres.quote_ident("bad\"name"),
            "\"bad\"\"name\""
        );
    }

    #[test]
    fn quote_ident_mysql() {
        assert_eq!(Dialect::Mysql.quote_ident("users"), "`users`");
        assert_eq!(Dialect::Mysql.quote_ident("bad`name"), "`bad``name`");
    }

    #[test]
    fn quote_ident_mssql() {
        assert_eq!(Dialect::Mssql.quote_ident("users"), "[users]");
        assert_eq!(Dialect::Mssql.quote_ident("bad]name"), "[bad]]name]");
    }

    #[test]
    fn supports_returning() {
        assert!(Dialect::Postgres.supports_returning());
        assert!(Dialect::Sqlite.supports_returning());
        assert!(!Dialect::Mysql.supports_returning());
        assert!(!Dialect::Mssql.supports_returning());
    }

    #[test]
    fn uses_output_returning() {
        assert!(Dialect::Mssql.uses_output_returning());
        assert!(!Dialect::Postgres.uses_output_returning());
        assert!(!Dialect::Mysql.uses_output_returning());
        assert!(!Dialect::Sqlite.uses_output_returning());
    }
}
