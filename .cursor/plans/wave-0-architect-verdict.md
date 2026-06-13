# Wave 0 Architect Verdict

**Author:** chief-architect (Wave 0 governance)  
**Date:** 2026-06-13  
**Inputs:** Existing IR (`crates/ferrum-core/src/ir/`), PyO3 bridge (`crates/ferrum-pyo3/src/lib.rs`), ARCHITECTURE §5.4 & §12, QUERY_ENGINE.md, ADR-002 canon (`.cursor/plans/adr-002-ir-contract.plan.md`)

---

## Verdict: **Aligned**

Wave 1 implementation may proceed. No architectural blockers. ADR-002 is canonized on the existing `IR_VERSION = 1` + JSON-over-PyO3 interim contract.

---

## ADR Sign-off Summary

| ADR | Topic | Wave 0 status | Implementation default |
|-----|-------|---------------|------------------------|
| **ADR-001** | Driver placement | Already decided | Python `asyncpg`; Rust off I/O path |
| **ADR-002** | QuerySet IR contract | **Canonized** | See `adr-002-ir-contract.plan.md`; code in `ir/mod.rs` is authoritative |
| **ADR-003** | Hydration semantics | **Confirmed** | Pydantic v2 `model_construct` (construct-without-revalidate) for DB-origin rows |
| **ADR-004** | Migration transactionality | **Confirmed** | Per-migration `BEGIN…COMMIT`; explicit per-step marking for non-transactional ops (`CREATE INDEX CONCURRENTLY`, certain `ALTER TYPE`) |
| **ADR-005** | Packaging / CI matrix | Leaning decided | maturin + cibuildwheel; abi3; Linux/macOS wheels; Windows sdist — finalize in Wave 5 |
| **ADR-006** | Error & hook boundary | **Confirmed** | Single Python `errors.py` + `hooks.py`; centralized SQLSTATE mapping, panic capture, Tier A/B/C redaction; non-bypassable default path |
| **ADR-007** | Migration confirmation token | Product decided | Dry-run-scoped token + `--confirm-environment`; no `--force`/`--yes` |
| **ADR-008** | `ferrum init` scope | Product decided | Local files only; 127.0.0.1 compose bind; no silent overwrite |

---

## ADR-002 Canonization Assessment

### What was reviewed

- `QuerySetIR`, `ModelMetadata`, `BindValue`, `Operation`, `FieldRef`, `Filter`, `OrderBy`, `SortDirection` in Rust with `serde` JSON mapping
- `compile_query(metadata_json, ir_json)` PyO3 entrypoint with panic catching
- `ferrum-core::compile` version check + allowlist validation
- `ferrum-sql::emit_select` parameterized emission (partial but directionally correct)

### Alignment with architecture invariants

| Invariant | Status |
|-----------|--------|
| Values out-of-band from identifiers | **Met** — `BindValue` in filters; `$N` placeholders in SQL |
| Field indices from metadata allowlists | **Met** — `FieldRef.index` + allowlist operator strings |
| Versioned IR with fail-fast | **Met** — `IR_VERSION = 1`; `IrVersionMismatch` error |
| Pure sync compile (GIL-held) | **Met** — no async in Rust boundary |
| JSON interim transport | **Met** — documented as v1 transport; binary deferred |

### Documented v1 gaps (Wave 1 work, not blockers)

These are **implementation backlog items**, not contract violations:

1. **Flat AND filters only** — `exclude` / `Q` / OR need predicate tree or lowering (QUERY_ENGINE §4.3); defer to Wave 1 Python builder + optional IR extension within v1.
2. **`count` / `exists` operations** — not yet in `Operation` enum; add additively in Wave 1–2.
3. **`fingerprint` missing** from `CompiledQuery` / PyO3 return dict — Wave 1 (Tier A hooks depend on it).
4. **Limit/offset as IR literals** — QUERY_ENGINE prefers bound params; v1 literals acceptable for developer-set integers; align in Wave 1 if needed without version bump.
5. **`is_null` operator emit** — currently binds placeholder incorrectly; Wave 1 emit fix.
6. **Python `_build_ir()` not implemented** — Wave 1 Track B; contract is defined.
7. **`compile/mod.rs` placeholder body** — returns empty SQL after validation; Wave 1 completes via `ferrum-sql`.

None of these require redesigning ADR-002; they are completion work against the canonized v1 schema.

---

## ADR-003 / ADR-004 / ADR-006 — Confirmed Defaults

### ADR-003 (Hydration)

- **Default:** `Model.model_construct(**payload)` for rows from PostgreSQL (trusted DB origin).
- **Custom validators:** Not re-run on read by default; document caveat for side-effect validators.
- **Rust role:** Decode asyncpg row representation → typed column map (`hydrate/mod.rs`).
- **Opt-in full validation:** Reserved for future config flag; not v0.1 Must-have.

### ADR-004 (Migration transactionality)

- **Default:** Wrap each migration step in `BEGIN…COMMIT` on Python orchestrator side.
- **Exceptions:** Steps marked non-transactional in plan metadata (`CREATE INDEX CONCURRENTLY`, enum/`ALTER TYPE` ops PostgreSQL forbids in a transaction block).
- **Failure output:** Must state whether DB changed, failed step, recovery action (PRD acceptance).
- **Separation:** ADR-004 = *how* statements run; ADR-007 = *whether* destructive/non-dev apply is authorized.

### ADR-006 (Error & hook boundary)

- **Single owner:** Python `errors.py` maps compile, asyncpg, SQLSTATE, PyO3 panics → Ferrum taxonomy.
- **Hooks:** Python `hooks.py` dispatches Tier A by default; Tier B/C require Ferrum-specific config keys (never `DEBUG=1` alone).
- **PyO3:** Panics → `FerrumInternalError`; compile `Err` → `FerrumCompileError`; no addresses/paths in messages.
- **Non-bypassable:** Query path must not emit hooks or user exceptions that skip redaction layer.

---

## Code Changes (Wave 0)

**None required** for IR contract alignment. The existing Rust types and JSON serde attributes **are** the canonized contract.

Optional documentation-only touch (deferred): update `ir/mod.rs` module comment from "ADR-002 in progress" to reference `adr-002-ir-contract.plan.md` — cosmetic; Wave 1 agents may do this when touching the file.

**Verification:** No code touched in Wave 0; `cargo check --workspace` not re-run (no diff).

---

## Blockers

**None.**

---

## Wave 1 Readiness

| Gate | Result |
|------|--------|
| ADR-002 canonized | ✅ |
| Backlog ordered (wave-0-backlog.md) | ✅ |
| Python/Rust can implement against shared JSON schema | ✅ |
| Undecided ADRs pre-empted? | ❌ No — ADR-005 packaging details remain open but do not block IR/compile work |
| Security-sensitive paths flagged | ✅ SQL compile, boundary, hooks — SecurityEngineer review at Wave 1 merge gate |

**Wave 1 can start.** Launch parallel tracks:

- **rust-core-engineer:** Complete compile + SQL emit + fingerprint  
- **python-orm-engineer:** ModelMetadata builder + `_build_ir()` matching v1 JSON  
- **test-engineer:** SQL-1/2/3 + QE-* test map  
- **product-designer:** API/DX review  

**Wave 1 merge gate:** chief-architect confirms Python JSON ↔ Rust deserializes and compiles; security-engineer reviews SQL emission.

---

## Risks to Monitor (non-blocking)

| Risk | Mitigation |
|------|------------|
| QUERY_ENGINE.md predicate tree diverges from flat v1 filters | Implement `exclude` via negated filter push in Python for v1; add tree when OR/`Q` lands |
| JSON bound_params double-encoding in PyO3 return | Wave 2: pass native Python objects across boundary instead of JSON strings in list |
| Limit/offset literal vs bound param | Document in ADR-002; security review confirms no user-string injection path |
| ADR-005 wheel matrix unset | Dev builds on Linux/macOS proceed; lock matrix before Wave 5 release |

---

## Artifacts Produced (Wave 0)

| File | Role |
|------|------|
| `.cursor/plans/wave-0-backlog.md` | PM ordered backlog |
| `.cursor/plans/adr-002-ir-contract.plan.md` | ADR-002 canon |
| `.cursor/plans/wave-0-architect-verdict.md` | This verdict |
