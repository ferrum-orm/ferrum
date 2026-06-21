//! SQL dialect configuration: placeholder style, identifier quoting, RETURNING support.

/// The syntax position and keyword used for returning inserted/updated rows.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ReturningSyntax {
    /// `RETURNING` clause at the very end of the statement (PostgreSQL, SQLite).
    Trailing,
    /// `OUTPUT` clause placed before `VALUES` or `WHERE` (MSSQL).
    Output,
    /// Dialect does not support returning rows from mutations natively (MySQL).
    None,
}

/// Supported SQL dialects for Ferrum's parameterized emitter.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Dialect {
    Postgres,
    Mysql,
    Sqlite,
    Mssql,
}

impl Dialect {
    /// Parse a dialect name from the `PyO3` boundary (`"postgres"`, `"mysql"`, `"sqlite"`).
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
            Self::Mssql => format!("[{}]", name.replace(']', "]]")),
        }
    }

    /// Format a column name for use in a RETURNING or OUTPUT clause.
    #[must_use]
    pub fn format_returning_field(&self, column_name: &str) -> String {
        match self {
            Self::Mssql => format!("inserted.{}", self.quote_ident(column_name)),
            _ => self.quote_ident(column_name),
        }
    }

    /// How this dialect supports returning rows from INSERT/UPDATE.
    #[must_use]
    pub fn returning_syntax(&self) -> ReturningSyntax {
        match self {
            Self::Postgres | Self::Sqlite => ReturningSyntax::Trailing,
            Self::Mssql => ReturningSyntax::Output,
            Self::Mysql => ReturningSyntax::None,
        }
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
        assert!(Dialect::parse("oracle").is_none());
    }

    #[test]
    fn placeholder_format() {
        assert_eq!(Dialect::Postgres.placeholder(1), "$1");
        assert_eq!(Dialect::Mysql.placeholder(1), "?");
        assert_eq!(Dialect::Sqlite.placeholder(42), "?");
        assert_eq!(Dialect::Mssql.placeholder(42), "?");
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
    fn returning_syntax() {
        assert_eq!(Dialect::Postgres.returning_syntax(), ReturningSyntax::Trailing);
        assert_eq!(Dialect::Sqlite.returning_syntax(), ReturningSyntax::Trailing);
        assert_eq!(Dialect::Mssql.returning_syntax(), ReturningSyntax::Output);
        assert_eq!(Dialect::Mysql.returning_syntax(), ReturningSyntax::None);
    }
}
