"""Source-cited consumer-parity manifest (production-readiness W0-B).

Every :class:`ParityEntry` documents one real call path from a consumer
application, cites the exact file/line/revision it was read from, and
classifies Ferrum's support for it. Classification is never inferred from a
method's name alone — each entry's ``evidence`` field says how the
classification was checked (existing integration test, a new executable
contract test in this directory, or a direct source read of the Ferrum
implementation named in ``ferrum_reference``).

Categories match the task contract's required inventory:
tenancy, concurrency, cancellation, pooling, type fidelity, migration
authority, and redaction — each entry is tagged with the primary category it
exercises via ``category``.

This module intentionally contains no Ferrum production code and no consumer
refactor. It is documentation with structural integrity checks (see
``test_manifest_integrity.py``).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

# Pinned revisions audited for this manifest. Re-audit and bump on drift.
TICKET_ANALYZER_REVISION = "ae7e262865db5d0472132ff5171770568dc79ae0"
ORG_AI_PLATFORM_REVISION = "561a46a1fe409d238068e02994e9c942b5cad706"

TICKET_ANALYZER_REPO = "ticket-analyzer-agent"
ORG_AI_PLATFORM_REPO = "org-ai-platform"


class Classification(StrEnum):
    """Ferrum's support status for one audited consumer call path."""

    SUPPORTED = "supported"
    """Ferrum has the API and it behaves correctly for the consumer's use."""

    FERRUM_DEFECT = "ferrum_defect"
    """Ferrum has the API but it behaves incorrectly for a real call shape."""

    MISSING_API = "missing_ferrum_api"
    """Ferrum has no API for this call path; the consumer cannot migrate to
    Ferrum for it without new Ferrum work."""

    CONSUMER_REFACTOR = "consumer_refactor"
    """Ferrum supports the underlying capability, but the consumer's current
    call shape needs to change (not Ferrum) to use it."""


@dataclass(frozen=True, slots=True)
class SourceCitation:
    """Exact provenance for one audited call path."""

    repo: str
    revision: str
    path: str
    lines: str
    excerpt: str

    def __post_init__(self) -> None:
        if ".." in self.path or self.path.startswith("/"):
            raise ValueError(f"Citation path must be repo-relative: {self.path!r}")
        if "-" not in self.lines:
            raise ValueError(f"Citation lines must be 'start-end': {self.lines!r}")
        start_str, end_str = self.lines.split("-", 1)
        start, end = int(start_str), int(end_str)
        if start < 1 or end < start:
            raise ValueError(f"Citation lines out of order: {self.lines!r}")
        if not self.excerpt.strip():
            raise ValueError("Citation excerpt must be a non-empty verbatim snippet")


@dataclass(frozen=True, slots=True)
class ParityEntry:
    """One classified, source-cited consumer call path."""

    id: str
    consumer: str
    category: str
    call_summary: str
    citation: SourceCitation
    classification: Classification
    ferrum_reference: str
    evidence: str
    notes: str = ""


CATEGORIES: frozenset[str] = frozenset(
    {
        "tenancy",
        "concurrency",
        "cancellation",
        "pooling",
        "type_fidelity",
        "migration_authority",
        "redaction",
    }
)

# ---------------------------------------------------------------------------
# Ticket Analyzer
# ---------------------------------------------------------------------------

_TA = TICKET_ANALYZER_REPO
_TA_REV = TICKET_ANALYZER_REVISION

_TICKET_ANALYZER_ENTRIES: tuple[ParityEntry, ...] = (
    ParityEntry(
        id="ta-01-rls-tenant-transaction",
        consumer=_TA,
        category="tenancy",
        call_summary=(
            "tenant_transaction(team_id) binds SET LOCAL app.team_id inside a "
            "Ferrum Transaction so RLS policies scope every query to one team."
        ),
        citation=SourceCitation(
            repo=_TA,
            revision=_TA_REV,
            path="packages/infra/src/infra/db/team_session.py",
            lines="1-70",
            excerpt=(
                "await session.execute(\n"
                "    text(\"SELECT set_config('app.team_id', CAST(:tid AS text), true)\"),"
            ),
        ),
        classification=Classification.SUPPORTED,
        ferrum_reference="ferrum.connection.Connection.tenant_transaction / Transaction.set_config",
        evidence=(
            "tests/python/integration/test_ticket_analyzer_compat.py::"
            "test_tenant_transaction_binds_team_guc exercises the same "
            "tenant_transaction + RLS GUC pattern against live PostgreSQL."
        ),
    ),
    ParityEntry(
        id="ta-02-platform-admin-bypass",
        consumer=_TA,
        category="tenancy",
        call_summary=(
            "platform_admin_session()/platform_admin_transaction() sets "
            "app.platform_admin=true so a platform-admin bypass RLS policy "
            "grants cross-team access for infra-owned scans."
        ),
        citation=SourceCitation(
            repo=_TA,
            revision=_TA_REV,
            path="packages/infra/src/infra/db/team_session.py",
            lines="1-90",
            excerpt=(
                "await session.execute(\n"
                "    text(\"SELECT set_config('app.platform_admin', 'true', true)\")"
            ),
        ),
        classification=Classification.SUPPORTED,
        ferrum_reference=(
            "Transaction.set_config(key, value) inside tenant_transaction(team_id=None)"
        ),
        evidence=(
            "New contract test "
            "test_ticket_analyzer_contracts.py::test_platform_admin_bypass_sees_all_teams "
            "proves a platform_admin-scoped transaction reads rows across two "
            "distinct team_id values that a plain RLS-scoped transaction cannot see."
        ),
    ),
    ParityEntry(
        id="ta-03-composite-pk",
        consumer=_TA,
        category="type_fidelity",
        call_summary=(
            "Ticket declares Meta.pk_fields-equivalent composite primary key "
            "(id, first_seen_at); get/update/delete must key on both columns."
        ),
        citation=SourceCitation(
            repo=_TA,
            revision=_TA_REV,
            path="packages/domain/src/domain/ticket.py",
            lines="62-66",
            excerpt=(
                "id: UUID = Field(default_factory=uuid4, primary_key=True)\n"
                "    team_id: UUID\n"
                '    team: ClassVar[ForeignKey] = ForeignKey(to="Team", '
                'on_delete="CASCADE")\n'
                "    helpshift_ticket_id: Annotated[str, Field(max_length=128)]\n"
                "    first_seen_at: datetime = Field(primary_key=True)"
            ),
        ),
        classification=Classification.SUPPORTED,
        ferrum_reference="Model.Meta.pk_fields; PRIMARY KEY (col1, col2) DDL",
        evidence=(
            "tests/python/integration/test_ticket_analyzer_compat.py::"
            "test_ticket_composite_pk_create_and_get covers create/get by "
            "composite key against live PostgreSQL."
        ),
    ),
    ParityEntry(
        id="ta-04-cas-update-returning-lease",
        consumer=_TA,
        category="concurrency",
        call_summary=(
            "webhook_events lease claim: filter(id=row_id, status='pending', "
            "attempts=current.attempts).filter(_UNLOCKED(now)).update_returning(...) "
            "is an optimistic-concurrency compare-and-swap (CAS) lease claim: zero "
            "rows returned means another worker already claimed the row."
        ),
        citation=SourceCitation(
            repo=_TA,
            revision=_TA_REV,
            path="packages/services/src/services/webhook_events_crud.py",
            lines="93-105",
            excerpt=(
                "rows = await (\n"
                "        WebhookEvent.objects.filter(\n"
                "            id=row_id,\n"
                '            status="pending",\n'
                "            attempts=current.attempts,\n"
                "        )\n"
                "        .filter(_UNLOCKED(now))\n"
                "        .update_returning(\n"
                "            tx,\n"
                "            attempts=current.attempts + 1,\n"
                "            locked_until=locked_until,\n"
                "        )\n"
                "    )"
            ),
        ),
        classification=Classification.SUPPORTED,
        ferrum_reference="QuerySet.update_returning(conn, **fields)",
        evidence=(
            "New contract test "
            "test_ticket_analyzer_contracts.py::test_cas_update_returning_lease_claim "
            "reproduces the exact filter-then-update_returning shape and proves a "
            "stale-attempts claim returns zero rows (lease already taken)."
        ),
    ),
    ParityEntry(
        id="ta-05-inbox-lease-unlocked-predicate",
        consumer=_TA,
        category="concurrency",
        call_summary=(
            "_UNLOCKED(now) = Q(locked_until__is_null=True) | Q(locked_until__lt=now) "
            "composes OR of two predicates, one of which is a NULL check, via Q()."
        ),
        citation=SourceCitation(
            repo=_TA,
            revision=_TA_REV,
            path="packages/services/src/services/webhook_events_crud.py",
            lines="21-28",
            excerpt=("return Q(locked_until__is_null=True) | Q(locked_until__lt=now)"),
        ),
        classification=Classification.SUPPORTED,
        ferrum_reference="ferrum.Q with & / | / ~ composition; __is_null / __lt operators",
        evidence=(
            "Covered by the same test_cas_update_returning_lease_claim contract "
            "test, which builds an equivalent Q()-composed unlocked predicate."
        ),
    ),
    ParityEntry(
        id="ta-06-jsonb-contains",
        consumer=_TA,
        category="type_fidelity",
        call_summary=(
            "alerts_crud.py filters the JSONB slack_delivery column with "
            "slack_delivery__contains={'ok': False}, which must compile to the "
            "JSONB @> containment operator, not a text LIKE."
        ),
        citation=SourceCitation(
            repo=_TA,
            revision=_TA_REV,
            path="packages/services/src/services/alerts_crud.py",
            lines="48-52",
            excerpt=(
                "Alert.objects.filter(\n"
                '            slack_delivery__contains={"ok": False},\n'
                "            created_at__lt=created_before,\n"
                "        )\n"
                '        .order_by("created_at")'
            ),
        ),
        classification=Classification.SUPPORTED,
        ferrum_reference="JSONB __contains -> @>, __has_key -> ?/?|",
        evidence=(
            "New contract test test_ticket_analyzer_contracts.py::"
            "test_jsonb_contains_filter_uses_containment_operator proves a "
            "__contains filter matches only rows whose JSONB column actually "
            "contains the probe value, live against PostgreSQL."
        ),
    ),
    ParityEntry(
        id="ta-07-uuid-array",
        consumer=_TA,
        category="type_fidelity",
        call_summary=(
            "Alert.ticket_ids: list[UUID] round-trips as a native PostgreSQL uuid[] array column."
        ),
        citation=SourceCitation(
            repo=_TA,
            revision=_TA_REV,
            path="packages/domain/src/domain/alert.py",
            lines="113-117",
            excerpt=(
                "ticket_ids: list[UUID] = Field(\n"
                "        default_factory=list,\n"
                "        nullable=False,\n"
                "        db_default=\"'{}'\",\n"
                "    )"
            ),
        ),
        classification=Classification.SUPPORTED,
        ferrum_reference="list[UUID] -> uuid[] DDL and bind type",
        evidence=(
            "tests/python/integration/test_ticket_analyzer_compat.py::"
            "test_alert_uuid_array_and_jsonb_round_trip already covers this "
            "round trip against live PostgreSQL."
        ),
    ),
    ParityEntry(
        id="ta-08-pgvector-nearest-to",
        consumer=_TA,
        category="type_fidelity",
        call_summary=(
            "nearest_tickets() filters summary_embedding__is_null=False then "
            "calls .nearest_to('summary_embedding', vector, metric='cosine') for "
            "pgvector-backed KNN similarity search with score projection."
        ),
        citation=SourceCitation(
            repo=_TA,
            revision=_TA_REV,
            path="packages/services/src/services/tickets_crud.py",
            lines="196-203",
            excerpt=(
                "Ticket.objects.filter(\n"
                "            team_id=team_id,\n"
                "            summary_embedding__is_null=False,\n"
                "        )\n"
                '        .nearest_to("summary_embedding", list(vector), metric="cosine")\n'
                "        .limit(limit)\n"
                "        .all(conn=tx)"
            ),
        ),
        classification=Classification.SUPPORTED,
        ferrum_reference="ferrum.ext.pgvector.vector_search / QuerySet.nearest_to",
        evidence=(
            "tests/python/integration/test_ticket_analyzer_compat.py::"
            "test_vector_search_returns_score_column and "
            "test_registered_vector_codec_decodes_on_every_pooled_connection "
            "already cover this against live PostgreSQL."
        ),
    ),
    ParityEntry(
        id="ta-09-bulk-upsert-static-fields",
        consumer=_TA,
        category="type_fidelity",
        call_summary=(
            "bulk_upsert_tickets() calls Ticket.objects.bulk_upsert(tx, rows, "
            "conflict_fields=[...], update_fields=[...static list...], "
            "batch_size=..., returning=False) to upsert a batch of tickets."
        ),
        citation=SourceCitation(
            repo=_TA,
            revision=_TA_REV,
            path="packages/services/src/services/tickets_crud.py",
            lines="1-300",
            excerpt=(
                "await Ticket.objects.bulk_upsert(\n"
                '    tx, rows, conflict_fields=["team_id", "helpshift_ticket_id"],\n'
                "    update_fields=list(TICKET_UPSERT_UPDATE_FIELDS), batch_size=batch_size,\n"
                "    returning=False,\n"
                ")"
            ),
        ),
        classification=Classification.SUPPORTED,
        ferrum_reference=(
            "QuerySet.bulk_upsert(conn, rows, conflict_fields, update_fields, "
            "batch_size, returning)"
        ),
        evidence=(
            "New contract test test_ticket_analyzer_contracts.py::"
            "test_bulk_upsert_batches_and_updates_conflicts proves a bulk_upsert "
            "call with a static update_fields list both inserts new rows and "
            "overwrites matching conflict rows, live against PostgreSQL. "
            "tests/python/unit/test_upsert.py covers the same API with a mocked "
            "connection only (SQL shape, not live-PG round trip)."
        ),
    ),
    ParityEntry(
        id="ta-10-streaming-chunks",
        consumer=_TA,
        category="pooling",
        call_summary=(
            "iter_ticket_chunks() opens query.stream(tx, chunk_size=...) as an "
            "async context manager and iterates cursor-pinned chunks without "
            "materializing the full result set."
        ),
        citation=SourceCitation(
            repo=_TA,
            revision=_TA_REV,
            path="packages/services/src/services/tickets_crud.py",
            lines="1-340",
            excerpt=(
                "async with query.stream(tx, chunk_size=min(chunk_size, limit)) as chunks:\n"
                "    async for chunk in chunks:\n"
                "        yield chunk"
            ),
        ),
        classification=Classification.SUPPORTED,
        ferrum_reference=(
            "QuerySet.stream(conn, chunk_size=...) cursor-pinning async context manager"
        ),
        evidence=(
            "New contract test test_ticket_analyzer_contracts.py::"
            "test_stream_yields_bounded_chunks_pinned_to_one_connection proves "
            "stream() yields chunk_size-bounded chunks summing to the full row "
            "count, live against PostgreSQL."
        ),
    ),
    ParityEntry(
        id="ta-11-call-function",
        consumer=_TA,
        category="migration_authority",
        call_summary=(
            "purge_team_retention_data() calls tx.call_function(_PURGE_FUNCTION, "
            "team_id, cutoff, batch_size) to invoke an allowlisted stored "
            "procedure rather than emitting ad hoc SQL."
        ),
        citation=SourceCitation(
            repo=_TA,
            revision=_TA_REV,
            path="packages/services/src/services/retention_crud.py",
            lines="21-26",
            excerpt=(
                "rows = await tx.call_function(\n"
                "        _PURGE_FUNCTION,\n"
                "        team_id,\n"
                "        cutoff,\n"
                "        batch_size,\n"
                "    )"
            ),
        ),
        classification=Classification.SUPPORTED,
        ferrum_reference="Transaction.call_function(name, *args) with allowlisted identifiers",
        evidence=(
            "tests/python/integration/test_ticket_analyzer_compat.py::"
            "test_call_function_round_trip already covers this against live "
            "PostgreSQL."
        ),
    ),
    ParityEntry(
        id="ta-12-nullable-predicate-filter-none",
        consumer=_TA,
        category="type_fidelity",
        call_summary=(
            "Consumer code must write filter(x__is_null=True) for NULL checks; "
            "filter(x=None) does not compile to IS NULL, an easy correctness "
            "trap for anyone porting Django-style ORM code (Django's filter(x=None) "
            "does emit IS NULL)."
        ),
        citation=SourceCitation(
            repo=_TA,
            revision=_TA_REV,
            path="packages/services/src/services/webhook_events_crud.py",
            lines="21-28",
            excerpt=("return Q(locked_until__is_null=True) | Q(locked_until__lt=now)"),
        ),
        classification=Classification.FERRUM_DEFECT,
        ferrum_reference="QuerySet.filter(**kwargs) / Q(**kwargs) equality-operator dispatch",
        evidence=(
            "New contract test test_ticket_analyzer_contracts.py::"
            "test_filter_equals_none_does_not_match_null_rows_defect reproduces "
            "the gap live: filter(x=None) returns zero rows (binds SQL NULL as a "
            "bound parameter to '=', which never matches per SQL three-valued "
            "logic) even though NULL rows exist and __is_null=True finds them. "
            "The consumer already avoids the trap by never writing filter(x=None) "
            "in this codebase, but the divergence from common ORM (Django/SQLAlchemy) "
            "expectations is a real Ferrum ergonomics defect worth a documented decision, "
            "not a silent gap."
        ),
        notes=(
            "Recommend either a documented refusal (raise on x=None) or "
            "Django-parity auto-IS-NULL translation for nullable columns; do "
            "not silently no-op."
        ),
    ),
    ParityEntry(
        id="ta-13-aggregate-group-by",
        consumer=_TA,
        category="type_fidelity",
        call_summary=(
            "Analytics/report paths need per-day and per-category ticket counts "
            "(ticket_counts_by_day, sql_aggregate(metric='by_category'|'by_severity')) "
            "which require GROUP BY plus a date_trunc-style bucketing expression."
        ),
        citation=SourceCitation(
            repo=_TA,
            revision=_TA_REV,
            path="packages/services/src/services/tickets_crud.py",
            lines="171-186",
            excerpt=(
                "async def aggregate_tickets(\n"
                "    tx: Transaction,\n"
                "    *,\n"
                "    predicates: Sequence[Q] = (),"
            ),
        ),
        classification=Classification.SUPPORTED,
        ferrum_reference="QuerySet.group_by(...) + aggregate(...) typed aggregates",
        evidence=(
            "New contract test test_ticket_analyzer_contracts.py::"
            "test_group_by_aggregate_counts_rows_per_bucket proves group_by + "
            "aggregate(count=...) returns correct per-bucket counts against a "
            "live table with rows in three distinct buckets."
        ),
    ),
    ParityEntry(
        id="ta-14-encrypted-bytea-credentials",
        consumer=_TA,
        category="redaction",
        call_summary=(
            "llm_provider_credentials_crud.py stores AES-256-GCM ciphertext + "
            "nonce bytea columns via raw AsyncSession.execute(text(...)) with an "
            "explicit ON CONFLICT DO UPDATE, deliberately bypassing Ferrum "
            "because Ferrum has no encrypted-codec field type and no upsert path "
            "that returns the generated id without exposing plaintext."
        ),
        citation=SourceCitation(
            repo=_TA,
            revision=_TA_REV,
            path="packages/services/src/services/llm_provider_credentials_crud.py",
            lines="117-136",
            excerpt=(
                "result = await session.execute(\n"
                "        text(\n"
                '            """\n'
                "            INSERT INTO llm_provider_credentials\n"
                "                (provider, ciphertext, nonce, key_version, "
                "base_url, enabled,\n"
                "                 last4, updated_by, updated_at)\n"
                "            VALUES\n"
                "                (:provider, :ciphertext, :nonce, :key_version, "
                ":base_url,\n"
                "                 :enabled, :last4, :updated_by, now())\n"
                "            ON CONFLICT (provider) DO UPDATE SET\n"
                "                ciphertext = EXCLUDED.ciphertext,"
            ),
        ),
        classification=Classification.MISSING_API,
        ferrum_reference=(
            "No ferrum.Field(codec=...) / encrypted bytea field type exists "
            "in python/ferrum/models.py"
        ),
        evidence=(
            "Direct source read of python/ferrum/models.py: _SUPPORTED_TYPES maps "
            'bytes -> "bytes"/BYTEA with no codec/encryption hook; no encrypted '
            "field type or transparent-encryption Field kwarg exists anywhere "
            "under python/ferrum/."
        ),
    ),
    ParityEntry(
        id="ta-15-migration-default-string-literal",
        consumer=_TA,
        category="migration_authority",
        call_summary=(
            "WebhookEvent.status is a Literal-backed enum column with "
            "db_default=\"'pending'\" — a quoted string SQL literal default on "
            "a Ferrum model field, the same shape Ticket Analyzer needs for "
            "every status/category-style column with a server-side default."
        ),
        citation=SourceCitation(
            repo=_TA,
            revision=_TA_REV,
            path="packages/domain/src/domain/webhook_event.py",
            lines="35-38",
            excerpt=(
                "    status: Annotated[\n"
                '        Literal["pending", "processed", "unroutable", "failed"],\n'
                "        Field(db_default=\"'pending'\", max_length=16),\n"
                '    ] = "pending"'
            ),
        ),
        classification=Classification.FERRUM_DEFECT,
        ferrum_reference=(
            "python/ferrum/migrations/orchestrator.py "
            "_DEFAULT_VALUE_ALLOWLIST (checked at CreateTable time, line ~409, "
            "and AlterColumn time, line ~520)"
        ),
        evidence=(
            "Direct source read of orchestrator.py: _DEFAULT_VALUE_ALLOWLIST is "
            "a fixed frozenset {NULL, TRUE, FALSE, NOW(), CURRENT_TIMESTAMP, "
            "CURRENT_DATE, CURRENT_TIME, GEN_RANDOM_UUID(), UUIDV7(), 0, 1, ''} "
            "with no general quoted-string-literal rule. "
            "_normalize_column_default(default).upper() not in that set raises "
            'FerrumMigrationError [FERR-M001] "Unsupported DEFAULT value". '
            "Reproduced live: applying a CreateTable op with "
            "ops.Column(..., default=\"'pending'\") against PostgreSQL raises "
            "exactly that error (see the _apply_schema comment in "
            "test_ticket_analyzer_contracts.py, which works around it by "
            "dropping the SQL-level default and requiring every ORM create() "
            "call to pass the field explicitly)."
        ),
        notes=(
            "Blocks any Ferrum-migrated consumer column whose Python default is "
            "a non-empty string with a server-side DEFAULT (status enums, "
            "category codes, etc.) — only the empty string default (\"''\") is "
            "allowlisted today. A fix should allow any single-quoted literal "
            "with no embedded quote-escape ambiguity, not just ''."
        ),
    ),
    ParityEntry(
        id="ta-16-migration-force-rls-never-enables",
        consumer=_TA,
        category="tenancy",
        call_summary=(
            "0018-force-rls.sql applies FORCE ROW LEVEL SECURITY as a defense-"
            "in-depth addition on tables that already have ENABLE ROW LEVEL "
            "SECURITY from an earlier migration, closing the gap where an "
            "app connecting as table owner bypasses team_isolation. A "
            "Ferrum-migrated version of this exact defense-in-depth step is "
            "ops.EnableRLS(table, force=True)."
        ),
        citation=SourceCitation(
            repo=_TA,
            revision=_TA_REV,
            path="migrations/0018-force-rls.sql",
            lines="3-20",
            excerpt=(
                "-- Defense-in-depth: FORCE Row Level Security on every "
                "tenant-scoped table.\n"
                "--\n"
                "-- RLS policies are NOT applied to a table's owner role unless "
                "the table\n"
                "-- is explicitly altered with FORCE ROW LEVEL SECURITY."
            ),
        ),
        classification=Classification.SUPPORTED,
        ferrum_reference=(
            "python/ferrum/migrations/orchestrator.py enable_rls op-to-SQL "
            "branch (EnableRLS operation in python/ferrum/migrations/"
            "operations.py) — W1-C fix: force=True now emits ENABLE then FORCE"
        ),
        evidence=(
            "W1-C resolved: orchestrator.py's enable_rls branch now emits "
            'both "ALTER TABLE ... ENABLE ROW LEVEL SECURITY" and '
            '"ALTER TABLE ... FORCE ROW LEVEL SECURITY" when force=True, '
            "so Postgres sets both relrowsecurity=true and "
            "relforcerowsecurity=true. Live-PostgreSQL contract test "
            "test_ticket_analyzer_contracts.py::"
            "test_force_rls_alone_enables_and_forces_rls verifies both "
            "pg_class flags and that a no-GUC query returns zero rows."
        ),
        notes=(
            "Security-relevant: SecurityEngineer review required per "
            "AGENTS.md §3 (migration-apply / RLS changes must not "
            "self-clear). W1-C closed the FORCE-only emission defect; "
            "the two-op workaround in this suite's fixtures is now "
            "redundant but kept for clarity."
        ),
    ),
)

# ---------------------------------------------------------------------------
# Org AI Platform
# ---------------------------------------------------------------------------

_OAI = ORG_AI_PLATFORM_REPO
_OAI_REV = ORG_AI_PLATFORM_REVISION

_ORG_AI_PLATFORM_ENTRIES: tuple[ParityEntry, ...] = (
    ParityEntry(
        id="oai-01-schema-per-tenant",
        consumer=_OAI,
        category="tenancy",
        call_summary=(
            "ShardRegistry resolves a per-tenant SQLAlchemy engine/session using "
            "schema_translate_map to route queries to tenant_<id> schemas within "
            "a shared database (a schema-per-tenant scheme), without changing "
            "SQL text per tenant."
        ),
        citation=SourceCitation(
            repo=_OAI,
            revision=_OAI_REV,
            path="backend/onyx/db/engine/async_sql_engine.py",
            lines="195-200",
            excerpt=(
                "schema_translate_map = {None: tenant_id}\n"
                "    async with engine.connect() as connection:\n"
                "        connection = await connection.execution_options(\n"
                "            schema_translate_map=schema_translate_map\n"
                "        )"
            ),
        ),
        classification=Classification.MISSING_API,
        ferrum_reference=(
            "No schema_transaction()/schema_translate_map equivalent in "
            "python/ferrum/connection.py or session.py"
        ),
        evidence=(
            "New contract test test_org_ai_platform_contracts.py::"
            "test_schema_per_tenant_routing_is_not_available_missing_api asserts "
            "ferrum.Connection/Transaction expose no schema-selection primitive "
            "(no attribute named schema_transaction, no with_schema, no "
            "schema_translate_map kwarg on connect/transaction); a repo-wide grep "
            "of python/ferrum/ confirms no such symbol exists."
        ),
        notes=(
            "Referenced in plan as a future explicit trusted shard router; "
            "QuerySet stays connection-explicit per AGENTS.md YAGNI constraint."
        ),
    ),
    ParityEntry(
        id="oai-02-shard-routing",
        consumer=_OAI,
        category="tenancy",
        call_summary=(
            "tenant_shard.py persists tenant->shard placement in a public.tenant_shard "
            "catalog table via raw text() SQL with ON CONFLICT (tenant_id) DO UPDATE "
            "(schema_translate_map does not rewrite text() SQL, so this table is "
            "always schema-qualified raw SQL) that a shard registry consults to "
            "route a tenant's queries to the correct physical shard database."
        ),
        citation=SourceCitation(
            repo=_OAI,
            revision=_OAI_REV,
            path="backend/onyx/db/tenant_shard.py",
            lines="29-39",
            excerpt=(
                "INSERT INTO public.tenant_shard (tenant_id, shard_name)\n"
                "                    VALUES (:tenant_id, :shard_name)\n"
                "                    ON CONFLICT (tenant_id)\n"
                "                    DO UPDATE SET shard_name = EXCLUDED.shard_name, "
                "updated_at = now()"
            ),
        ),
        classification=Classification.MISSING_API,
        ferrum_reference=(
            "No multi-DSN ConnectionRegistry/ShardRouter type anywhere under python/ferrum/"
        ),
        evidence=(
            "Direct source read: python/ferrum/connection.py exposes a single "
            "Connection per DSN with no registry/router abstraction for routing "
            "by tenant key across multiple physical databases. The catalog-table "
            "upsert itself (ON CONFLICT DO UPDATE against a single-column key) is "
            "independently provable as supported once schema/shard routing exists; "
            "see oai-08 for that sub-capability tested in isolation."
        ),
    ),
    ParityEntry(
        id="oai-03-select-for-update-skip-locked",
        consumer=_OAI,
        category="concurrency",
        call_summary=(
            "_claim_next_processing_file()/_claim_next_deleting_file() use "
            ".with_for_update(skip_locked=True) to let multiple workers race for "
            "queued rows without blocking on rows already locked by a peer."
        ),
        citation=SourceCitation(
            repo=_OAI,
            revision=_OAI_REV,
            path="backend/onyx/background/task_utils.py",
            lines="71-77",
            excerpt=(
                "select(UserFile.id)\n"
                "        .where(UserFile.status == UserFileStatus.PROCESSING)\n"
                "        .order_by(UserFile.created_at)\n"
                "        .limit(1)\n"
                "        .with_for_update(skip_locked=True)"
            ),
        ),
        classification=Classification.MISSING_API,
        ferrum_reference=(
            "No QuerySet.select_for_update()/lock() method in python/ferrum/queryset.py"
        ),
        evidence=(
            "New contract test test_org_ai_platform_contracts.py::"
            "test_select_for_update_skip_locked_is_not_available_missing_api "
            "asserts QuerySet has no select_for_update/for_update/lock method; a "
            "repo-wide grep of python/ferrum/ for 'select_for_update' and 'FOR "
            "UPDATE' confirms no such capability is emitted anywhere in the "
            "SQL-compilation path."
        ),
        notes=(
            "Ticket Analyzer's CAS/update_returning lease pattern (ta-04) is a "
            "viable Ferrum-native substitute for this exact row-lock use case and "
            "should be the recommended migration path, not a new lock primitive, "
            "absent a demonstrated need for true row blocking."
        ),
    ),
    ParityEntry(
        id="oai-04-with_for_update-nowait",
        consumer=_OAI,
        category="concurrency",
        call_summary=(
            "document.py locks a document row with .with_for_update(nowait=True) "
            "so a concurrent writer fails fast with a lock-not-available error "
            "instead of blocking, then the caller decides how to retry."
        ),
        citation=SourceCitation(
            repo=_OAI,
            revision=_OAI_REV,
            path="backend/onyx/db/document.py",
            lines="1370-1373",
            excerpt=(
                "select(DbDocument.id)\n"
                "        .where(DbDocument.id.in_(document_ids))\n"
                "        .with_for_update(nowait=True)"
            ),
        ),
        classification=Classification.MISSING_API,
        ferrum_reference=(
            "Same missing capability as oai-03; no nowait lock variant exists either"
        ),
        evidence=(
            "Same absence proof as oai-03 "
            "(test_select_for_update_skip_locked_is_not_available_missing_api); "
            "nowait and skip_locked are both FOR UPDATE row-lock modifiers "
            "Ferrum's compiler never emits."
        ),
    ),
    ParityEntry(
        id="oai-05-multihop-relation-filter",
        consumer=_OAI,
        category="type_fidelity",
        call_summary=(
            "document_set.py chains multiple .join() calls across "
            "DocumentSet -> DocumentSet__ConnectorCredentialPair -> "
            "ConnectorCredentialPair to filter document sets by a nested "
            "connector/credential attribute, a two-hop relation filter."
        ),
        citation=SourceCitation(
            repo=_OAI,
            revision=_OAI_REV,
            path="backend/onyx/db/document_set.py",
            lines="621-635",
            excerpt=(
                "select(DocumentSetDBModel, ConnectorCredentialPair)\n"
                "        .join(\n"
                "            DocumentSet__ConnectorCredentialPair,\n"
                "            DocumentSetDBModel.id\n"
                "            == DocumentSet__ConnectorCredentialPair.document_set_id,\n"
                "            isouter=True,"
            ),
        ),
        classification=Classification.CONSUMER_REFACTOR,
        ferrum_reference=(
            "filter(a__b=...) one-level relation lookups only; nested a__b__c "
            "hops are rejected by design"
        ),
        evidence=(
            "Direct source read of AGENTS.md/CHANGELOG: Ferrum's relation-filter "
            "JOINs are documented as one-level Django-style lookups only "
            "(filter(team__slug=...)); nested a__b__c hops and FTS on relation "
            "lookups are explicitly rejected by _check_write_scope-adjacent "
            "validation. A consumer using Ferrum for this table would need to "
            "flatten the two-hop join into one explicit select_related/filter "
            "hop or a join-table-scoped subquery -- a consumer-side query "
            "reshape, not a Ferrum defect or a missing primitive, since one-hop "
            "relation filters are supported."
        ),
    ),
    ParityEntry(
        id="oai-06-pydantic-type-decorator",
        consumer=_OAI,
        category="type_fidelity",
        call_summary=(
            "pydantic_type.py defines a generic SQLAlchemy TypeDecorator that "
            "serializes/deserializes an arbitrary Pydantic BaseModel to/from a "
            "JSONB column, so model fields can be typed as nested Pydantic "
            "models rather than raw dict."
        ),
        citation=SourceCitation(
            repo=_OAI,
            revision=_OAI_REV,
            path="backend/onyx/db/pydantic_type.py",
            lines="9-10",
            excerpt="class PydanticType(TypeDecorator):\n    impl = JSONB",
        ),
        classification=Classification.MISSING_API,
        ferrum_reference=(
            'python/ferrum/models.py _SUPPORTED_TYPES maps only dict -> "json"; '
            "no BaseModel-subclass annotation support"
        ),
        evidence=(
            "New contract test test_org_ai_platform_contracts.py::"
            "test_nested_pydantic_model_field_falls_back_to_text_type_missing_api "
            "reproduces the gap live: declaring a Ferrum Model field annotated "
            "with a nested Pydantic BaseModel subclass does not raise and does "
            "not map to JSONB — python/ferrum/models.py's "
            '`_SUPPORTED_TYPES.get(base_type, "text")` silently falls back to '
            "a plain TEXT column for any annotation it does not recognize, so "
            "the failure mode is a wrong DDL type (and an eventual bind-type "
            "error when a real BaseModel instance is written to a TEXT column), "
            "not a documented rejection at class-definition time. There is no "
            "equivalent to SQLAlchemy's PydanticType TypeDecorator today."
        ),
        notes=(
            "The silent text-type fallback for any unrecognized annotation (not "
            "just nested BaseModel) is worth flagging to ChiefArchitect "
            "independently of this specific gap — an unrecognized type should "
            "fail fast at class-definition time, not silently become TEXT."
        ),
    ),
    ParityEntry(
        id="oai-07-encrypted-kv-conditional-upsert",
        consumer=_OAI,
        category="redaction",
        call_summary=(
            "entities.py's transfer_entity upserts KGEntity via "
            "pg_insert(...).on_conflict_do_update(...) where the SET clause mixes "
            "an additive expression (occurrences=KGEntity.occurrences + "
            "entity.occurrences), a JSONB merge operator, and "
            "func.coalesce(KGEntity.entity_key, entity.entity_key) -style "
            "column expressions, i.e. an upsert whose UPDATE SET clause is a "
            "per-column SQL expression referencing the existing row, not a "
            "static value list."
        ),
        citation=SourceCitation(
            repo=_OAI,
            revision=_OAI_REV,
            path="backend/onyx/db/entities.py",
            lines="124-136",
            excerpt=(
                ".on_conflict_do_update(\n"
                '            index_elements=["name", "entity_type_id_name", '
                '"document_id"],\n'
                "            set_=dict(\n"
                "                occurrences=KGEntity.occurrences + "
                "entity.occurrences,\n"
                '                attributes=KGEntity.attributes.op("||")(\n'
                "                    literal(entity.attributes, JSONB)\n"
                "                ),\n"
                "                entity_key=func.coalesce(KGEntity.entity_key, "
                "entity.entity_key),"
            ),
        ),
        classification=Classification.MISSING_API,
        ferrum_reference=(
            "QuerySet.upsert/bulk_upsert(update_fields=[...]) only accepts a "
            "static column-name list, not an expression"
        ),
        evidence=(
            "New contract test test_org_ai_platform_contracts.py::"
            "test_bulk_upsert_cannot_express_conditional_coalesce_update proves, "
            "live against PostgreSQL, that Ferrum's bulk_upsert always overwrites "
            "an update_fields column with the new row's value (including "
            "overwriting a non-NULL existing value with an incoming NULL), so it "
            "cannot express transfer_entity's 'keep existing value unless the "
            "incoming batch actually provides a new one' COALESCE semantics, "
            "nor its additive occurrences=old+new SET expression, without first "
            "reading and merging in Python."
        ),
    ),
    ParityEntry(
        id="oai-08-encrypted-kv-static-upsert",
        consumer=_OAI,
        category="redaction",
        call_summary=(
            "encrypted_kv_store.py's upsert_encrypted_kv is a plain "
            "pg_insert(...).on_conflict_do_update(index_elements=['key'], "
            "set_={'value': stmt.excluded.value}) with a static field, unlike "
            "the expression-based SET clause in oai-07."
        ),
        citation=SourceCitation(
            repo=_OAI,
            revision=_OAI_REV,
            path="backend/onyx/db/encrypted_kv_store.py",
            lines="21-26",
            excerpt=(
                "stmt = pg_insert(EncryptedKeyValueStore).values(key=key, "
                "value=value)\n"
                "        db_session.execute(\n"
                "            stmt.on_conflict_do_update(\n"
                '                index_elements=["key"], '
                'set_={"value": stmt.excluded.value}\n'
                "            )\n"
                "        )"
            ),
        ),
        classification=Classification.SUPPORTED,
        ferrum_reference=("QuerySet.upsert(conflict_fields=['key'], update_fields=['value'])"),
        evidence=(
            "Covered by the same live-PG proof as ta-09 "
            "(test_bulk_upsert_batches_and_updates_conflicts): a static "
            "single-column conflict target with a static update_fields list is "
            "the supported case; only the COALESCE-expression variant (oai-07) "
            "is missing."
        ),
    ),
    ParityEntry(
        id="oai-09-fastapi-users-auth-persistence",
        consumer=_OAI,
        category="pooling",
        call_summary=(
            "users.py wires fastapi_users_db_sqlalchemy.SQLAlchemyUserDatabase "
            "directly onto an AsyncSession for FastAPI-Users auth persistence "
            "(user CRUD, password hash storage, OAuth account table)."
        ),
        citation=SourceCitation(
            repo=_OAI,
            revision=_OAI_REV,
            path="backend/onyx/auth/users.py",
            lines="2036-2038",
            excerpt=(
                "user_db: SQLAlchemyUserDatabase[User, uuid.UUID] = "
                "SQLAlchemyUserDatabase(\n"
                "        async_db_session, User, OAuthAccount\n"
                "    )"
            ),
        ),
        classification=Classification.MISSING_API,
        ferrum_reference="No ferrum.contrib.fastapi_users adapter under python/ferrum/",
        evidence=(
            "Direct source read: python/ferrum has a ferrum.contrib.fastapi "
            "lifespan/Depends helper (ferrum_lifespan, get_ferrum_conn) but no "
            "fastapi-users SQLAlchemyUserDatabase-compatible adapter; adopting "
            "Ferrum for this path today requires either keeping SQLAlchemy for "
            "the auth tables only (a defensible split, matching how ticket-"
            "analyzer-agent keeps Better Auth on Drizzle) or a new Ferrum "
            "contrib adapter implementing fastapi_users.db.base.BaseUserDatabase."
        ),
    ),
    ParityEntry(
        id="oai-10-alembic-multitenant-drift",
        consumer=_OAI,
        category="migration_authority",
        call_summary=(
            "alembic/env.py sets include_schemas=True and reads "
            "CURRENT_TENANT_ID_CONTEXTVAR to select a per-tenant schema for "
            "autogenerate/upgrade, and to compare live per-tenant schema state "
            "against models for drift before migrating a shard."
        ),
        citation=SourceCitation(
            repo=_OAI,
            revision=_OAI_REV,
            path="backend/alembic/env.py",
            lines="255-263",
            excerpt=(
                "context.configure(\n"
                "        connection=connection,\n"
                "        target_metadata=target_metadata,\n"
                "        version_table_schema=schema_name,\n"
                "        include_schemas=True,"
            ),
        ),
        classification=Classification.SUPPORTED,
        ferrum_reference=(
            "ferrum.migrations.drift.detect_drift(conn, models, schema=<name>) read-only comparison"
        ),
        evidence=(
            "New contract test test_org_ai_platform_contracts.py::"
            "test_detect_drift_compares_a_named_non_public_schema proves, live "
            "against PostgreSQL, that detect_drift(conn, models, schema='tenant_x') "
            "correctly reports a missing/extra column against a table created in "
            "a non-public schema -- so the per-schema read-only fidelity check "
            "Onyx's Alembic env.py wants is a supported Ferrum primitive today. "
            "Ferrum has no Alembic-equivalent autogenerate/apply-DDL step -- "
            "numbered SQL stays authoritative per AGENTS.md -- so the migration "
            "*apply* half of this workflow (not audited here) remains a consumer "
            "responsibility either way."
        ),
        notes=(
            "Classification covers the read-only drift/fidelity-check half only, "
            "matching the task's explicit 'migration-authority' failure-mode "
            "inventory, not full Alembic-autogenerate parity."
        ),
    ),
)

MANIFEST: tuple[ParityEntry, ...] = _TICKET_ANALYZER_ENTRIES + _ORG_AI_PLATFORM_ENTRIES
