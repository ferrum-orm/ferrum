//! PostgreSQL dialect configuration: placeholder style, identifier quoting, type names.

/// Render a positional parameter placeholder for PostgreSQL.
/// PostgreSQL uses `$1`, `$2`, …
#[must_use]
pub fn placeholder(position: usize) -> String {
    format!("${position}")
}

/// Quote an identifier (table or column name) for PostgreSQL.
/// Doubles any embedded double-quotes to prevent injection via metadata names.
/// Identifiers come exclusively from model metadata allowlists — this is a
/// defense-in-depth measure, not the primary guard.
#[must_use]
pub fn quote_ident(name: &str) -> String {
    format!("\"{}\"", name.replace('"', "\"\""))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn placeholder_format() {
        assert_eq!(placeholder(1), "$1");
        assert_eq!(placeholder(42), "$42");
    }

    #[test]
    fn quote_ident_simple() {
        assert_eq!(quote_ident("users"), "\"users\"");
    }

    #[test]
    fn quote_ident_escapes_embedded_quotes() {
        assert_eq!(quote_ident("bad\"name"), "\"bad\"\"name\"");
    }
}
