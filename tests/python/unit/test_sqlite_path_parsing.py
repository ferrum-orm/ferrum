"""Unit tests for SQLite DSN-to-path parsing."""

from ferrum.drivers.sqlite import _sqlite_path_from_dsn


class TestSqlitePathParsing:
    """Test _sqlite_path_from_dsn handles various DSN formats correctly."""

    def test_memory_with_netloc(self) -> None:
        assert _sqlite_path_from_dsn("sqlite://:memory:") == ":memory:"
        assert _sqlite_path_from_dsn("sqlite://:memory:/") == ":memory:"

    def test_memory_empty_path(self) -> None:
        assert _sqlite_path_from_dsn("sqlite://") == ":memory:"
        assert _sqlite_path_from_dsn("sqlite:///") == ":memory:"

    def test_relative_path_three_slashes(self) -> None:
        # sqlite:///relative/path -> path="/relative/path" (keep as-is for relative)
        assert _sqlite_path_from_dsn("sqlite:///test.db") == "/test.db"
        assert _sqlite_path_from_dsn("sqlite:///relative/test.db") == "/relative/test.db"

    def test_absolute_path_four_slashes(self) -> None:
        # sqlite:////absolute/path -> path="//absolute/path" -> "/absolute/path"
        assert _sqlite_path_from_dsn("sqlite:////tmp/test.db") == "/tmp/test.db"  # noqa: S108
        assert (
            _sqlite_path_from_dsn("sqlite:////home/runner/work/_temp/ferrum_test.db")
            == "/home/runner/work/_temp/ferrum_test.db"
        )

    def test_netloc_based_relative(self) -> None:
        # sqlite://relative/path -> netloc="relative", path="/path"
        assert _sqlite_path_from_dsn("sqlite://relative/test.db") == "relative/test.db"

    def test_percent_encoded_path(self) -> None:
        # URL-encoded characters should be unquoted
        assert _sqlite_path_from_dsn("sqlite:///test%20file.db") == "/test file.db"
        assert (
            _sqlite_path_from_dsn("sqlite:////tmp/test%20file.db")  # noqa: S108
            == "/tmp/test file.db"  # noqa: S108
        )
