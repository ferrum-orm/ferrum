# Bugs Found During Driver Exercise

This document tracks bugs discovered while running comprehensive driver tests.

## BUG #1: Model Meta attribute naming inconsistency

**Severity:** High (breaks ORM functionality)  
**Component:** Core ORM / Model metadata  
**Discovered in:** SQLite driver testing  

### Symptom
Models fail with `FerrumSchemaError: Schema object not found (OperationalError). [FERR-S001]` even when tables exist in the database.

### Root Cause
The Model `Meta` class must use `table = "table_name"` not `table_name = "table_name"`.

### Example
```python
# WRONG - causes schema object not found error
class Author(Model):
    id: int
    name: str
    
    class Meta:
        table_name = "authors"  # ❌ WRONG

# CORRECT
class Author(Model):
    id: int
    name: str
    
    class Meta:
        table = "authors"  # ✅ CORRECT
```

### Impact
- Documentation/examples may show the wrong attribute name
- Users copying from examples would hit this immediately
- Error message is misleading (says "schema object not found" rather than "incorrect meta attribute")

### Recommendation
1. Update documentation to consistently use `Meta.table`
2. Consider adding a validation error if `Meta.table_name` is used
3. Improve error message to detect this common mistake

---

## Status by Driver

### SQLite
- **Tests Run:** 15
- **Passed:** 13
- **Failed:** 0
- **Skipped:** 2 (transactions not supported - expected)
- **Status:** ✅ All tests passing

### PostgreSQL
- **Status:** Not tested yet (no database available)

### MySQL  
- **Status:** Not tested yet (no database available)

### SQL Server
- **Status:** Not tested yet (no database available)

---

## Test Coverage

The driver exercise suite tests:
- ✅ CRUD operations (create, read, update, delete)
- ✅ Bulk operations (bulk_create)
- ✅ Filtering (simple filters, Q objects)
- ✅ Ordering (ascending, descending)
- ✅ Pagination (limit, offset)
- ✅ NULL handling (create with NULL, filter by NULL)
- ✅ Foreign keys
- ✅ Composite primary keys
- ✅ Transaction commit/rollback (PostgreSQL only)
- ✅ Unique constraint violations
- ✅ Capability gates (proper errors for unsupported features)
- ✅ Column types: int, text, bool, decimal, date, timestamp, UUID
