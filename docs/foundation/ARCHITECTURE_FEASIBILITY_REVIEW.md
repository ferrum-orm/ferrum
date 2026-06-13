# Ferrum PRD — Architecture Feasibility Review

**Reviewer:** Chief Architect
**Artifact under review:** `docs/foundation/PRODUCT_REQUIREMENTS.md` (commit `6e58a3e`, scope-resolution `c1bfced`)
**Companion inputs:** `docs/design/PRODUCT_DESIGN_REVIEW.md` (GUY-84/GUY-89), `docs/foundation/SECURITY_REVIEW_PRD.md` (GUY-85/GUY-88/GUY-91)
**Issue:** [GUY-92](/GUY/issues/GUY-92) · **Parent:** [GUY-63](/GUY/issues/GUY-63)
**Date:** 2026-06-13

---

## 1. Verdict

**Feasible to proceed into the architecture phase (`ARCHITECTURE.md` and the GUY-63 artifact set), conditionally.**

The PRD describes a coherent, deliberately narrow v0.1 (async-only, PostgreSQL-only, Pydantic v2 models, Rust/PyO3 SQL engine, guarded migrations, tiered observability). Nothing in it is technically infeasible. The product scope is well-bounded and the security requirements are testable. The Rust/PyO3 boundary, async execution model, and migration safety model are all achievable with mature, off-the-shelf tooling.

The verdict is **conditional** because the architecture must resolve a small set of decisions that materially change the engineering shape of the system. None of these block *starting* the architecture documents — they are the decisions those documents must make. **Two items require a Product Manager micro-revision** (Section 5) before engineering ramps, and **one item should be escalated to the CEO** as a build/tooling cost decision (Section 6).

I am **not** returning this PRD to the PM as a feasibility blocker. Product scope for v0.1 stands.

---

## 2. Review Method

Applied architecture lenses, cited inline:

- **CAP Theorem** — single-primary PostgreSQL; consistency-favoring single-node semantics.
- **Single Responsibility / Separation of Concerns** — Python orchestration vs. Rust compilation boundary.
- **Blast Radius** — failure isolation of the Rust core, the connection pool, and migration apply.
- **Evolutionary Architecture** — contract stability across the PyO3 boundary and schema-evolution headroom for v0.2 relationships.
- **Least Astonishment** — Django/Pydantic mental-model fidelity.
- **Event-Driven vs Request-Response** — observability hooks as synchronous callbacks vs. async fan-out.
- **Data Gravity** — PostgreSQL anchoring; hydration locality (where typed objects are built).
- **Observability First** — Tier A/B/C contract as a launch gate.
- **YAGNI** — guarding against speculative relationship/sharding machinery in v0.1.
- **Defense in Depth** — allowlist compilation + parameterization + sanitized error mapping as layered controls.
- **Schema Evolution** — additive-by-default migration classification and the destructive gate.

---

## 3. Feasibility by Review Scope

### 3.1 Rust/PyO3 boundary — FEASIBLE

The PRD asks Rust to own SQL compilation, hydration, schema analysis, error mapping, and migration planning, exposed via PyO3. This is the standard, proven shape (PyO3 + `maturin`; precedent: `pydantic-core`, `tokenizers`, `polars`).

**Architecture must decide (ADR-level), but feasibility is not in question:**

- **Boundary granularity (Single Responsibility, Least Astonishment).** The cleanest cut is: Rust is a **pure, synchronous, stateless compiler/codec** — `QuerySet IR (in) → {sql_text, bound_params, param_type_summary} (out)` and `rows (in) → hydrated payload (out)`. Python owns the async runtime, the connection pool, transactions, and hook dispatch. This keeps Rust off the async I/O path entirely and makes the GIL question tractable (Section 4.1).
- **Driver placement (Data Gravity, Blast Radius).** Where the actual PostgreSQL socket lives is the single highest-leverage decision in the whole architecture. Two viable shapes — see **ADR-001** below. This is the one item I recommend escalating cost-wise (Section 6).
- **IR ownership.** The QuerySet→IR boundary should be a versioned, serializable contract (Evolutionary Architecture). Recommend a typed struct boundary over loose dicts so the v0.2 relationship work is additive.

**No feasibility blocker.** Required ADRs listed in Section 7.

### 3.2 Async execution & concurrency — FEASIBLE, with one hard constraint

The PRD's strongest concurrency requirements (L245–246, L305): *"Concurrent compilation reuses immutable state and never mutates shared request data"* and *"Concurrent awaits on independent queries do not mutate shared compiled state."*

These are satisfiable **iff** the architecture enforces the discipline that **compiled state is immutable and per-call**. The natural design:

- Model metadata (allowlists, column maps) is built **once** at model-class definition time and is thereafter **read-only**. Concurrency lens: shared-immutable is safe to share across tasks without locks.
- Compilation produces a **fresh, owned** output per call; nothing in the request path mutates the shared metadata. This is trivially true if the Rust compiler is a pure function over `(&Metadata, QuerySetIR)`.
- **Cancellation/timeout (Least Astonishment, Blast Radius):** must be owned on the Python/async side at the driver await point, mapped to a deterministic `TimeoutError`/`CancelledError` Ferrum category (PRD L329, L332). If a synchronous Rust call holds the GIL, it is **not cancellable** mid-flight — therefore compilation must be fast and bounded, and **all cancellable waiting must happen in Python around I/O**, never inside a long Rust call. This reinforces "Rust = pure fast compiler, Python = async I/O."

**Hard constraint for `ARCHITECTURE.md`:** no per-request mutable shared state may live in the Rust layer. State that must persist across calls (the connection pool, migration ledger) lives in Python or in an explicitly synchronized, clearly-owned component. **This is an architecture invariant, not a PRD problem.**

### 3.3 PostgreSQL-only connection strategy & migration safety — FEASIBLE

- **Single-target PostgreSQL (CAP, YAGNI).** Single-primary, consistency-favoring. No multi-DB, no replication control, no proxy. This is the *simplifying* choice and is fully feasible. TLS/`sslmode` is an architecture-phase doc gap the security review already flagged as non-blocking; I will own it in the connection-strategy ADR.
- **Migration atomicity (Schema Evolution, Blast Radius).** PostgreSQL supports **transactional DDL** — most schema changes can run inside `BEGIN…COMMIT` with full rollback on failure. This directly answers the designer's §6.2.2 question (Section 4.2). The PRD requirement that failure output state "whether the database changed" (L185) is satisfiable precisely because PG gives us transactional DDL for the common cases; the architecture must enumerate the **non-transactional exceptions** (e.g., `CREATE INDEX CONCURRENTLY`) and document them as out-of-transaction with explicit recovery guidance.
- **Destructive gates & danger APIs.** All product-level, all feasible. The classification table (drop column/table, type narrowing, `NOT NULL` on populated column) maps cleanly to a diff-classifier in the Rust planner with a Python-side confirmation gate. Unscoped `delete()`/`update()` → `danger_delete_all()`/`danger_update_all()` is a Python API guard (fail before IR emission).

### 3.4 Security-sensitive implications — FEASIBLE, security already cleared

Security granted clearance three times (GUY-85/88/91). From an architecture standpoint these requirements are **structurally satisfiable and, in fact, reinforce a clean design**:

- **Identifier allowlisting (Defense in Depth).** Compile-time resolution from immutable model metadata is the *same* mechanism that gives us the concurrency-safe immutable-metadata property. One mechanism, two wins.
- **Parameterization.** Bound params are the only value path; the Rust compiler never sees a reason to interpolate. Enforced by making the IR carry values out-of-band from identifiers.
- **Sanitized error mapping (Blast Radius).** A single centralized error-mapping layer at the Python boundary converts PG `SQLSTATE` + driver errors + PyO3 panics into the stable Ferrum taxonomy. This is the one place that must be hardened; it is a well-contained component.
- **Tiered observability (Observability First).** Tier A/B/C is a hook-payload-shaping contract. Tier A is the default emitter; B/C are opt-in builders gated behind Ferrum-specific config (never `DEBUG=1`). Architecture must define the **hook payload schema as a stable contract** and the redaction layer as non-bypassable for the default path.

### 3.5 Packaging risk — FEASIBLE, but the real cost center

A Python package with a Rust core means **per-platform wheels** (manylinux x86_64/aarch64, macOS x86_64/arm64, Windows), `abi3` for forward-compatible CPython, an sdist fallback that needs a Rust toolchain, and CI matrix cost. This is **the highest-friction operational risk in the project** (PRD L490, L508 explicitly leaves "packaging targets" open). It is solved (maturin + cibuildwheel) but it is real work and a real CI bill. See **ADR-005** and the CEO escalation (Section 6).

---

## 4. Answers to Open Technical Questions (Product Design §6.2)

### 4.1 PyO3 exception mapping & GIL/latency (§6.2.1)

- **GIL:** Compilation is CPU-bound and short. Keep the Rust compile call **synchronous and holding the GIL** — releasing/reacquiring the GIL for sub-millisecond work costs more than it saves, and it sidesteps the cancellation hazard. The async win comes from I/O, not from compilation. Latency budget (<100ms per the designer, comfortably <1ms realistically for compilation) is met.
- **Exceptions:** Rust returns `Result<_, FerrumCompileError>`; PyO3 maps the `Err` arm to a typed Python exception **without** trace strings or addresses. We must set `panic = "unwind"` (not `abort`) for the extension and wrap the boundary so a Rust panic becomes a catchable Python exception (PRD L160–161, L168; ERR-3). **No process abort in normal operation** is achievable and must be a boundary test.
- **No "huge trace string" copy:** error payloads carry structured fields (model, field, operator, category), not formatted blobs. Formatting happens in Python at display time.

### 4.2 Migration atomicity after partial failure (§6.2.2)

- **Yes**, leverage PostgreSQL transactional DDL: wrap each migration in `BEGIN…COMMIT`. Failure → `ROLLBACK` → "database unchanged." This satisfies L177/L185 for the common path.
- **Exceptions** the architecture must enumerate and special-case: `CREATE INDEX CONCURRENTLY`, certain `ALTER TYPE`/enum operations, and anything PG forbids inside a transaction block. For those, the migration step is marked non-transactional, runs outside the wrapping transaction, and the failure message must state the exact pre/post state and recovery action. **This becomes ADR-004.**

### 4.3 Hydration path (§6.2.3)

- **Recommended:** Rust fetches/receives raw rows and constructs typed payloads handed to Python, with Pydantic v2 model construction using the **validation-skipping fast path** for trusted DB-origin data (DB already enforced types/constraints) — re-running full Pydantic validation on every hydrated row is the performance trap. This keeps the hot loop tight and the Doherty <400ms target reachable.
- **Open tradeoff for the data-modeling ADR:** "construct-without-revalidate" assumes the DB column types are the source of truth and that we trust the round-trip. If a model has custom Pydantic validators with side effects, skipping them changes semantics (Least Astonishment risk). Architecture must decide the default and document it. This is **ADR-003**.

---

## 5. Product Requirements Needing PM Revision (Non-Blocking, but required before engineering)

These are **product-contract gaps the design review already surfaced (§6.1)** and that the PRD itself leaves open. They are small, but engineers cannot implement against them as written:

1. **Non-interactive destructive-migration confirmation (CI/CD).** The PRD mandates "explicit environment confirmation" for non-dev applies (L176, L184) and the design proposes an interactive "type the DB name" flow (§4.3). **There is no specified non-interactive contract for headless CI/CD.** A bare `--force` is an anti-pattern the designer correctly rejected. PM must specify the product contract (e.g., a dry-run-hash confirmation token `--confirm-destructive=<sha256-of-dryrun-plan>` or an explicit env var + plan-hash pairing). **Architecture can propose the mechanism, but PM owns whether headless destructive apply is even in v0.1 scope.** → returns to ProductManager.

2. **`ferrum init` / quickstart scaffolding scope (the 30-minute promise).** Success Criteria L397 commits to "under 30 minutes" and the design proposes a `ferrum init` that scaffolds config + `docker-compose.yml` (§6.1). The PRD lists CLI migration commands but does **not** confirm an `init`/`dev-db` command exists in v0.1. This affects CLI surface and architecture (do we own a CLI bootstrap?). PM must confirm v0.1 CLI scope. → returns to ProductManager.

Everything else in the PRD is implementable as written. I am flagging these two via comment to the ProductManager rather than editing product scope myself (per my role boundaries).

---

## 6. CEO Escalation (Cost/Risk)

**Packaging & CI matrix for the Rust core.** This is a recurring cost and a release-reliability risk, and the PRD explicitly defers "packaging targets" to architecture (L508). Recommendation to bring to the CEO as an explicit decision:

- **Scope of supported wheels for v0.1.** Proposal: Linux (manylinux x86_64 + aarch64) and macOS (arm64 + x86_64) using `abi3` (build once per platform, run on CPython 3.x), **defer Windows wheels** to v0.2 (sdist-only on Windows for v0.1). This roughly halves CI matrix cost and is consistent with the FastAPI/Starlette server audience (predominantly Linux/macOS).
- **Why escalate:** wheel matrix breadth is a cost (CI minutes) and reliability (more targets = more release breakage) decision that trades off addressable audience. It fits the "technology/build decisions with cost implications" escalation bar in my charter.

I will raise this as a board approval after the architecture connection-strategy ADR (ADR-001) lands, since the driver choice influences the build matrix (a pure-Rust driver vs. binding to `libpq` changes the native-dependency story).

---

## 7. Required ADRs (to be authored in `DECISIONS.md` during architecture)

| ADR | Decision | Why it matters (lens) | Default leaning |
|-----|----------|------------------------|-----------------|
| **ADR-001** | PostgreSQL driver placement: async driver in **Python** (e.g., asyncpg) with Rust as pure compiler/codec, **vs.** Rust-side async driver (sqlx/tokio-postgres) bridged to the Python event loop | Data Gravity, Blast Radius, Async model | **Python-side driver (asyncpg)** — keeps Rust off the async I/O path, simplest cancellation story, smallest native-dependency surface, avoids a second runtime (tokio) under the GIL. Strangler-friendly: Rust can absorb the driver later if benchmarks demand it. |
| **ADR-002** | QuerySet→Rust **IR contract** shape (typed struct boundary vs. dict) and version stability | Evolutionary Architecture, Least Astonishment | Typed, versioned IR; values carried out-of-band from identifiers (enforces parameterization + allowlisting structurally). |
| **ADR-003** | Hydration semantics: Pydantic v2 **construct-without-revalidate** for DB-origin rows vs. full validation | Performance (Doherty), Least Astonishment | Construct fast-path by default; document the trusted-source assumption and custom-validator caveat. |
| **ADR-004** | Migration **transactionality** model and the non-transactional exception list | Schema Evolution, Blast Radius | Per-migration `BEGIN…COMMIT`; enumerate + special-case `CONCURRENTLY`/enum/`ALTER TYPE` exceptions with explicit recovery text. |
| **ADR-005** | **Packaging targets** & CI wheel matrix for v0.1 | Operational cost/risk | maturin + cibuildwheel; abi3; Linux+macOS wheels v0.1, Windows sdist-only (pending CEO sign-off, Section 6). |
| **ADR-006** | **Error-mapping & hook-payload contract** as a single centralized, non-bypassable boundary layer | Defense in Depth, Observability First | One Python boundary component owns SQLSTATE→Ferrum taxonomy, PyO3 panic capture, and Tier A/B/C redaction. |

---

## 8. Risks & Tradeoffs (architecture-level)

- **Two-runtime hazard.** If ADR-001 chooses a Rust-side async driver, we introduce a tokio runtime alongside asyncio under one GIL — a real complexity and debuggability cost. Default leaning avoids it. (Blast Radius, Observability First.)
- **Hydration fast-path vs. semantic fidelity.** ADR-003's performance win can surprise users relying on custom validators. Mitigation: explicit default + documented opt-in to full validation. (Least Astonishment.)
- **Packaging breakage.** Native wheels break more often than pure-Python packages; mitigated by a constrained matrix and an sdist fallback, but it is ongoing release toil. (Section 6.)
- **Migration recovery without rollback migrations (v0.1).** PRD defers rollback to forward-migrations + manual recovery (L501). Acceptable for v0.1 given transactional DDL covers the common failure path, but the non-transactional exceptions are where operators can get stuck — ADR-004 must make recovery text first-class. (Schema Evolution.)
- **CAP framing.** Single-primary PG = CP under partition (unavailable rather than inconsistent). Correct and intentional for an ORM; documented so downstream teams don't expect multi-region availability. (CAP.)

---

## 9. Disposition & Handoff

- **Feasibility: GRANTED (conditional).** Architecture phase (GUY-63 artifact set) may begin. No PRD feasibility blocker; v0.1 product scope stands.
- **Conditions:** the six ADRs in Section 7 must be decided in `ARCHITECTURE.md`/`DECISIONS.md`; the Section 4 invariants (immutable per-call Rust state, GIL-holding pure compiler, transactional DDL) must be honored.
- **Returns to ProductManager (non-blocking micro-revisions):** §5.1 non-interactive destructive-confirmation contract; §5.2 `ferrum init`/CLI quickstart scope.
- **CEO escalation (queued):** packaging/CI wheel-matrix scope (Section 6), to be raised alongside ADR-001.
- **SecurityEngineer:** architecture will deliver the two architecture-phase follow-ups Security assigned to me — SQL-compilation + migration-apply threat model, and TLS/`sslmode` production connection hardening docs. No new security PRD gaps found in this review.
- **Engineers (Backend):** do not start implementation until `ARCHITECTURE.md` lands with ADR-001/002/006 resolved (these define the boundary contract you build against). Implementation that bypasses architecture review will be flagged to the CEO per charter.

---

*Produced by Chief Architect for [GUY-92](/GUY/issues/GUY-92). Source PRD commit `6e58a3e`; scope resolution `c1bfced`.*
