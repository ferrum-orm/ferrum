//! SQL dialect configuration: placeholder style, identifier quoting, RETURNING support.

/// Supported SQL dialects for Ferrum's parameterized emitter.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Dialect {
    Postgres,
    Mysql,
    Sqlite,
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
            _ => None,
        }
    }

    /// Render a positional parameter placeholder for this dialect.
    #[must_use]
    pub fn placeholder(&self, position: usize) -> String {
        match self {
            Self::Postgres => format!("${position}"),
            Self::Mysql | Self::Sqlite => "?".to_string(),
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
        }
    }

    /// Whether this dialect supports `RETURNING` on INSERT/UPDATE.
    #[must_use]
    pub fn supports_returning(&self) -> bool {
        matches!(self, Self::Postgres | Self::Sqlite)
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
        assert!(Dialect::parse("oracle").is_none());
    }

    #[test]
    fn placeholder_format() {
        assert_eq!(Dialect::Postgres.placeholder(1), "$1");
        assert_eq!(Dialect::Mysql.placeholder(1), "?");
        assert_eq!(Dialect::Sqlite.placeholder(42), "?");
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
    fn supports_returning() {
        assert!(Dialect::Postgres.supports_returning());
        assert!(Dialect::Sqlite.supports_returning());
        assert!(!Dialect::Mysql.supports_returning());
    }
}
