# Ferrum Driver Exercise Suite

Comprehensive test suite that exercises all Ferrum database drivers against real databases.

## Supported Drivers

1. **PostgreSQL** (asyncpg) - Full feature set
2. **MySQL** (asyncmy) - Thin parity: FTS, composite PKs
3. **SQLite** (aiosqlite) - Thin parity: RETURNING, FTS, composite PKs
4. **SQL Server** (aioodbc) - Thin parity: FTS, composite PKs

## What This Tests

- **CRUD Operations**: Create, read, update, delete
- **Bulk Operations**: Bulk create
- **Filtering**: Simple filters, Q objects, complex predicates
- **Ordering**: Ascending and descending sorts
- **Pagination**: Limit and offset
- **NULL Handling**: Creating and filtering NULL values
- **Relationships**: Foreign keys, composite primary keys
- **Transactions**: Commit and rollback (PostgreSQL only)
- **Error Handling**: Unique constraint violations
- **Capability Gates**: Ensures unsupported features raise proper errors
- **Column Types**:
  - Integers (primary keys, counters)
  - Text (VARCHAR, TEXT)
  - Booleans
  - Decimals (NUMERIC)
  - Dates and Timestamps
  - UUIDs
  - JSON/JSONB (where supported)

## Prerequisites

### Python Dependencies

```bash
# Install ferrum with all drivers
pip install 'ferrum-orm[pg,mysql,sqlite,mssql]'

# Or from source
cd /workspace
uv sync --extra dev
maturin develop
```

### System Dependencies (for SQL Server)

SQL Server requires the ODBC driver:

```bash
# Ubuntu/Debian
curl https://packages.microsoft.com/keys/microsoft.asc | sudo tee /etc/apt/trusted.gpg.d/microsoft.asc
curl https://packages.microsoft.com/config/ubuntu/$(lsb_release -rs)/prod.list | sudo tee /etc/apt/sources.list.d/mssql-release.list
sudo apt-get update
sudo ACCEPT_EULA=Y apt-get install -y msodbcsql18

# macOS
brew tap microsoft/mssql-release https://github.com/Microsoft/homebrew-mssql-release
brew install msodbcsql18
```

## Running the Tests

### 1. Start the databases

```bash
docker-compose up -d

# Wait for health checks
docker-compose ps
```

### 2. Set environment variables (optional)

Copy `.env.example` to `.env` and adjust if needed:

```bash
cp .env.example .env
```

Default DSNs:
- PostgreSQL: `postgresql://ferrum_test:ferrum_test@localhost:5432/ferrum_test`
- MySQL: `mysql://ferrum_test:ferrum_test@localhost:3306/ferrum_test`
- SQLite: `sqlite:///test.db` (file-based, no container needed)
- SQL Server: `mssql://ferrum_test:FerrumTest123@localhost:1433/ferrum_test`

### 3. Run the exercise suite

```bash
python exercise_drivers.py
```

### 4. Clean up

```bash
docker-compose down -v
rm -f test.db  # Remove SQLite database
```

## Testing Individual Drivers

To test only specific drivers, comment out the unwanted configurations in `exercise_drivers.py` or set the DSN env vars to empty strings:

```bash
# Test only PostgreSQL
export FERRUM_TEST_MYSQL_DSN=""
export FERRUM_TEST_SQLITE_DSN=""
export FERRUM_TEST_MSSQL_DSN=""
python exercise_drivers.py
```

## Expected Results

### PostgreSQL
- All tests should pass (full feature set)

### MySQL
- CRUD, filtering, ordering, pagination, NULL handling, foreign keys, composite PKs should pass
- Transactions should be skipped (thin parity)
- Capability gates should pass (raises proper errors for unsupported features)

### SQLite
- Similar to MySQL
- RETURNING clause is supported
- File-based, no container needed

### SQL Server
- Similar to MySQL and SQLite
- Requires ODBC driver installation
- Uses different SQL syntax but same Ferrum API

## Troubleshooting

### Connection Failures

```bash
# Check container health
docker-compose ps

# View logs
docker-compose logs postgres
docker-compose logs mysql
docker-compose logs mssql

# Restart services
docker-compose restart
```

### SQL Server ODBC Issues

```bash
# Check if ODBC driver is installed
odbcinst -q -d

# Should show:
# [ODBC Driver 18 for SQL Server]

# Test connection manually
sqlcmd -S localhost -U ferrum_test -P FerrumTest123 -C -Q "SELECT @@VERSION"
```

### Port Conflicts

If you already have databases running on default ports, edit `docker-compose.yml` to use different host ports:

```yaml
ports:
  - "5433:5432"  # PostgreSQL
  - "3307:3306"  # MySQL
  - "1434:1433"  # SQL Server
```

Then update DSNs accordingly.

## Test Output Example

```
Comprehensive Ferrum Driver Exercise Suite
============================================================

============================================================
Testing POSTGRES
============================================================
  ✓ create
  ✓ read
  ✓ update
  ✓ delete
  ✓ bulk_create
  ✓ filtering
  ✓ ordering
  ✓ pagination
  ✓ null_handling
  ✓ foreign_keys
  ✓ composite_pk
  ✓ transaction_commit
  ✓ transaction_rollback
  ✓ unique_constraint_violation
  ✓ capability_gates

============================================================
Testing MYSQL
============================================================
  ✓ create
  ✓ read
  ...
  ⊘ transaction_commit: Driver does not support transactions
  ⊘ transaction_rollback: Driver does not support transactions
  ✓ capability_gates

============================================================
SUMMARY
============================================================
postgres: 15/15 passed, 0 failed, 0 skipped
mysql: 13/15 passed, 0 failed, 2 skipped
sqlite: 13/15 passed, 0 failed, 2 skipped
mssql: 13/15 passed, 0 failed, 2 skipped
```

## Next Steps

- Add to CI: This suite can be added to CI pipelines to catch driver regressions
- Extend coverage: Add more complex relationship tests, aggregation tests, etc.
- Performance benchmarking: Measure query execution times across drivers
- Migration testing: Test schema migration operations
