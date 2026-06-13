# Ferrum development Makefile
# Mirrors the CI jobs so local verification matches the gate.
#
# Smallest-verification convention (PROJECT_STRUCTURE.md §8.1):
#   Rust-only change:       make test-rust lint-rust
#   Python-only change:     make dev test-python-unit
#   Extension/boundary:     make dev test-integration test-security
#   Full local CI parity:   make ci-local

.PHONY: dev lint-rust lint-python type-python check-rust boundary \
        test-rust test-python-unit test-integration test-security \
        test-property all-tests ci-local clean

# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

dev:
	maturin develop

dev-release:
	maturin develop --release

# ---------------------------------------------------------------------------
# Linting
# ---------------------------------------------------------------------------

lint-rust:
	cargo fmt --all --check
	cargo clippy --workspace --all-targets -- -D warnings

lint-python:
	ruff check python/ tests/
	ruff format --check python/ tests/

lint: lint-rust lint-python

# ---------------------------------------------------------------------------
# Type checking
# ---------------------------------------------------------------------------

type-python:
	mypy python/ferrum --strict

# ---------------------------------------------------------------------------
# Rust checks
# ---------------------------------------------------------------------------

check-rust:
	cargo check --workspace

boundary:
	@echo "Checking ferrum-core: no pyo3/tokio..."
	@cargo tree -p ferrum-core --edges normal | grep -E "pyo3|tokio" && echo "FAIL" && exit 1 || echo "OK"
	@echo "Checking ferrum-sql: no pyo3/tokio..."
	@cargo tree -p ferrum-sql --edges normal | grep -E "pyo3|tokio" && echo "FAIL" && exit 1 || echo "OK"

# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

test-rust:
	# ferrum-core and ferrum-sql only: ferrum-pyo3 is a cdylib and is tested
	# via `maturin develop + pytest` (Python integration tests).
	cargo test -p ferrum-core -p ferrum-sql

test-python-unit:
	pytest tests/python/unit tests/python/property -v

test-integration: dev
	pytest tests/python/integration -m integration -v

test-security: dev
	pytest tests/python/security -m security -v

test-property:
	pytest tests/python/property -m property -v

all-tests: dev test-rust test-python-unit test-security

# ---------------------------------------------------------------------------
# Import boundary
# ---------------------------------------------------------------------------

import-boundary:
	lint-imports

# ---------------------------------------------------------------------------
# Full local CI parity
# ---------------------------------------------------------------------------

ci-local: lint check-rust boundary test-rust type-python import-boundary all-tests

# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

clean:
	cargo clean
	rm -rf target/ dist/ wheelhouse/ .pytest_cache/ .ruff_cache/ .mypy_cache/
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
