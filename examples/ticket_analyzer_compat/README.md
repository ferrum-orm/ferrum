# ticket-analyzer compatibility fixture

End-to-end acceptance models and a hand-written migration that exercise Ferrum
features required to migrate **ticket-analyzer-agent**:

| Pattern | Ferrum surface |
|--------|----------------|
| Composite primary key (`id` + `first_seen_at`) | `Field(primary_key=True)` on multiple fields |
| Issue deduplication | `QuerySet.upsert()` with `ON CONFLICT (team_id, dedup_key)` |
| `uuid[]` + JSONB on alerts | `list[UUID]` and `dict` field types |
| Tenant isolation | `tenant_transaction()` + RLS policies in migration |
| pgvector similarity | `ferrum.ext.pgvector.vector_search()` score column |
| Stored procedures | `Connection.call_function()` |
| Extensions / RLS / functions in migrations | `CreateExtension`, `EnableRLS`, `CreatePolicy`, `CreateFunction`, `AddIndex` (HNSW) |

## Layout

- `models.py` — `Team`, `Ticket`, `Issue`, `Alert`
- `migrations/0001_ticket_analyzer_compat.py` — manual migration using `ferrum.migrations.operations`

## Live integration tests

```bash
export FERRUM_TEST_DSN="postgresql://user:pass@localhost/ferrum_test"
mise run dev
pytest -m integration tests/python/integration/test_ticket_analyzer_compat.py
```

The integration module builds an isolated schema per test run (suffix tables) while
this example keeps stable `ta_compat_*` table names for local experimentation.
