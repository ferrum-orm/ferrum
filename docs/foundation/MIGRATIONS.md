# Ferrum Migrations

**Status:** Proposed — pending CEO/board approval
**Version:** v0.1 migration contract
**Inputs:** [DATA_MODELING.md](./DATA_MODELING.md), [ARCHITECTURE.md](./ARCHITECTURE.md), [PRODUCT_REQUIREMENTS.md](./PRODUCT_REQUIREMENTS.md), [ARCHITECTURE_FEASIBILITY_REVIEW.md](./ARCHITECTURE_FEASIBILITY_REVIEW.md), [SECURITY_REVIEW_PRD.md](./SECURITY_REVIEW_PRD.md)
**Issue:** [GUY-73](/GUY/issues/GUY-73)
**Blocked by:** [GUY-70](/GUY/issues/GUY-70) (DATA_MODELING.md — done)
**Date:** 2026-06-13

---

## 1. Purpose & Scope

This document specifies how Ferrum turns a set of `Model` classes into a safe, deterministic, reviewable sequence of PostgreSQL schema changes. It is the authoritative migration contract engineers implement against. It defines:

- the **migration philosophy** (what is auto-detected, what is manual, squashing policy);
- the **schema diffing algorithm** (introspected DB state vs. model state → an ordered operation plan);
- the **migration file format** (Python-based operation objects);
- **forward and rollback execution semantics** (transactional wrapper, per-operation inverse, non-transactional exceptions);
- the **migration history table** (the ledger that prevents double-apply and detects drift);
- **conflict detection** for parallel branch merges.

It binds to and does **not** restate the data-model contract. The single source of truth for *what a schema looks like* is `ModelMetadata` ([DATA_MODELING.md §4](./DATA_MODELING.md)); this document is the single source of truth for *how schema state changes over time*. Where this document and the architecture/PRD disagree, the upstream documents win and the conflict is flagged, not silently resolved.

### 1.1 What this builds on (do not duplicate)

| Already decided upstream | Where | This doc's job |
|--------------------------|-------|----------------|
| Immutable, introspectable `ModelMetadata` (columns, constraints, indexes, allowlists) | [DATA_MODELING.md §4](./DATA_MODELING.md) | Consume it as the **target** side of the diff |
| Type map (`int→BIGINT`, `str→TEXT/VARCHAR(n)`, …) | [DATA_MODELING.md §3.2](./DATA_MODELING.md) | Reuse verbatim for DDL emission and narrowing classification |
| Constraint/index declaration + deterministic index naming | [DATA_MODELING.md §5, §6](./DATA_MODELING.md) | Emit DDL + classify destructiveness |
| Per-migration `BEGIN…COMMIT` + non-transactional exception list | [ARCHITECTURE.md ADR-004](./ARCHITECTURE.md) | Define the executor, recovery text, per-step marking |
| Non-interactive confirmation token | [ARCHITECTURE.md ADR-007](./ARCHITECTURE.md) | Define the planner digest + token binding it consumes |
| `ferrum init` scaffold (files only) | [ARCHITECTURE.md ADR-008](./ARCHITECTURE.md) | Out of scope here; referenced only as the entry point |
| Migration ledger as Python-owned append-only log | [ARCHITECTURE.md §9.3, §10.2](./ARCHITECTURE.md) | Specify the table design here |
| Hook events `migration_*` + tier contract | [ARCHITECTURE.md §13](./ARCHITECTURE.md) | Map each lifecycle stage to a hook + tier |

### 1.2 Lenses applied

- **Schema Evolution** — additive-by-default; destructive operations are gated, never silent. Diffs are deterministic for the same `(DB state, model state)` pair.
- **Blast Radius** — a failed migration must leave the database either fully changed or fully unchanged for the common path; partial states are enumerated, marked, and given recovery text.
- **Defense in Depth** — every identifier reaching DDL comes from the metadata allowlist built at class-definition time; destructive applies are gated by an opaque, live-state-bound token. Layers stack: classification → dry-run → confirmation → transactional execution → ledger.
- **Least Astonishment** — Django-shaped commands and file format; deterministic naming so the same models produce the same plan and the same index/constraint names across machines.
- **Observability First** — every plan generation and apply emits a `migration_*` hook (Tier A/B) before launch is allowed; dry-run output is reviewable and secret-free.
- **YAGNI** — no automatic data backfill DSL, no migration squashing UI, no multi-DB dialect abstraction in v0.1.
- **CAP framing** — apply prefers *unavailability over inconsistency*: on any drift/mismatch the planner refuses to apply rather than apply a stale plan.

### 1.3 Out of v0.1 scope (explicit guard)

Engineers **must not** build these in v0.1 (they are deferred seams, not work):

- Down-migrations as the *primary* recovery mechanism. v0.1 recovery is **transactional auto-rollback within a step** plus **forward fix-up migrations** ([ARCHITECTURE_FEASIBILITY_REVIEW.md §8](./ARCHITECTURE_FEASIBILITY_REVIEW.md): "PRD defers rollback to forward-migrations + manual recovery"). Per-operation inverse semantics are still **defined** (§6) because they are what the transactional wrapper rolls back to and what a future down-migration would reuse — but no standalone `migrate down N` command ships in v0.1.
- Automatic data migrations / backfills beyond DDL (e.g. populating a new `NOT NULL` column from a computed expression). A `RunPython`-style data-op shape is reserved in §5.5 as a forward contract only.
- Automatic squash tooling. Squashing is a **manual, opt-in** operation in v0.1 (§2.3).
- Any non-PostgreSQL dialect. The planner and emitter are PostgreSQL-specific ([ARCHITECTURE.md §15.2](./ARCHITECTURE.md)).

---

## 2. Migration Philosophy

### 2.1 Auto-detect, human-reviewed, explicitly-applied

Ferrum's stance is **auto-generation under mandatory human review**, not full automation and not fully-manual authoring.

```text
models change ──► makemigrations ──► migration file (auto-generated, committed) ──► review (PR) ──► dry-run ──► apply
                  (auto-detect)        (durable, version-controlled)               (human/CI)    (token)   (gated)
```

| Stage | Mode | Rationale (lens) |
|-------|------|------------------|
| Diff detection | **Automatic** — diff `ModelMetadata` against introspected DB | Least Astonishment (Django parity); Schema Evolution (deterministic) |
| Migration file | **Generated, then committed** to the repo as durable source | Evolutionary Architecture (the plan is reviewable, diffable history, not ephemeral) |
| Review | **Mandatory** — the generated operations are human-readable Python | Defense in Depth (a human sees `DropColumn` before it runs) |
| Apply | **Explicitly invoked + gated** — never auto-applied on import or startup | Blast Radius (no surprise DDL on deploy) |

**Why generated-and-committed (not generated-on-the-fly at apply time):** committing the migration file makes the plan an auditable artifact, gives PR review a concrete diff to approve, and lets the confirmation token (§7) bind to *a reviewed plan*, not a freshly-recomputed one. Auto-applying a freshly-diffed plan at deploy time would defeat the entire review gate (rejected alternative, §10).

**Editable, but lint-checked.** Because operations are plain Python objects, an engineer may hand-edit a generated migration (e.g. to add a data step, reorder, or add a deliberate manual op). The planner does **not** treat generated files as read-only. Apply re-validates that the file's operations are well-formed and that identifiers still resolve against current metadata allowlists; it does not require the file to byte-match what `makemigrations` would produce today.

### 2.2 What is auto-detected (v0.1)

The diff engine (§3) detects and emits operations for all of the following, satisfying the GUY-73 acceptance criterion *"auto-detection covers add/remove/alter column and index changes"*:

| Change class | Auto-detected operations | Destructive? |
|--------------|--------------------------|--------------|
| Table | `CreateTable`, `DropTable`, `RenameTable`† | Drop/narrow = destructive |
| Column add | `AddColumn` (nullable or with default) | No |
| Column add (NOT NULL, no default, populated table) | `AddColumn` flagged | **Destructive** (would fail/lock) |
| Column remove | `DropColumn` | **Destructive** |
| Column type change | `AlterColumnType` | **Destructive iff narrowing** (§3.4) |
| Column nullability | `SetNotNull` / `DropNotNull` | `SetNotNull` on populated = **destructive** |
| Column default | `SetDefault` / `DropDefault` | No |
| Constraint | `AddConstraint` / `DropConstraint` (unique, check, FK) | Drop = destructive-adjacent (§3.4) |
| Index | `CreateIndex` / `DropIndex` | No (drop is reversible by recreate) |

† `RenameTable`/`RenameColumn` cannot be inferred from a structural diff alone (a rename is indistinguishable from drop+add). v0.1 detects them only via an **explicit rename hint** the engineer supplies; absent the hint, the diff yields a destructive drop + an additive add and the dry-run labels it loudly. This is the Least-Astonishment-safe default (never silently treat a drop as a rename).

### 2.3 Squashing policy

- **Squashing is manual and opt-in in v0.1.** `ferrum migrations squash <from> <to>` collapses a contiguous, already-applied range into a single equivalent migration, preserving the net schema effect.
- The squashed migration records the range it **replaces** (`replaces: [...]` in its metadata). The ledger (§4) and apply logic treat a squash as satisfied if *either* the squashed migration *or* its full replaced set is recorded as applied — so squashing never re-runs work on environments that already applied the originals.
- Squashing does **not** delete history; the original files remain in the repo until an engineer prunes them after every environment has advanced past the squash point. This is the Strangler-Fig-safe sequence: introduce the squash, let all environments cross it, then retire the originals.
- **No automatic squash trigger** (no "squash when N > 50"). That is YAGNI for v0.1 and risks rewriting history under operators' feet.

### 2.4 When NOT to use auto-detection

Auto-detection is structural. Two model states that are structurally identical but semantically different (a rename, or a column whose *meaning* changed but type did not) require explicit operator intent. The philosophy is: **the diff proposes; the human disposes.** Anything the diff cannot prove safe is surfaced as destructive or as a required hint, never auto-resolved.

---

## 3. Schema Diffing Algorithm

The planner is a **pure function** over two introspectable inputs and produces a deterministic, ordered operation plan. It runs in Rust (`plan_migration`, [ARCHITECTURE.md §8.6](./ARCHITECTURE.md)); Python owns introspection I/O, classification gating, and apply.

```text
plan = diff(target_state, current_state)

target_state  = canonical schema derived from all registered ModelMetadata   (no I/O; from class definitions)
current_state = canonical schema introspected from the live database         (Python reads catalogs; passed to Rust as data)
plan          = ordered [Operation], each tagged {destructive, transactional}
```

### 3.1 Inputs

- **Target (model) state.** Built from the frozen `ModelMetadata` of every registered concrete model ([DATA_MODELING.md §4](./DATA_MODELING.md)). Already an allowlist of validated identifiers, columns, constraints, indexes, with deterministic index/constraint names ([DATA_MODELING.md §6.2](./DATA_MODELING.md)). **No user strings**; identifiers are pre-validated metadata.
- **Current (DB) state.** Introspected by the Python layer from PostgreSQL catalogs (`information_schema` / `pg_catalog`: `pg_class`, `pg_attribute`, `pg_constraint`, `pg_index`, `pg_type`). The introspector normalizes catalog rows into the **same canonical schema shape** as the target so the diff compares like-with-like. Introspection reads only the migration-managed schema (default `public`); objects Ferrum did not create are ignored unless they collide by name (§3.5).

Both sides are reduced to a canonical, order-independent representation (sorted column lists, normalized type spellings via the §3.2 map, canonicalized check/partial-index predicates) so the diff is **deterministic and stable** regardless of declaration order or catalog row ordering — satisfying the PRD requirement *"migration results are deterministic for the same schema state."*

### 3.2 Type canonicalization (reuses the data model's closed type map)

Comparison happens on **canonical PostgreSQL types**, derived once from the [DATA_MODELING.md §3.2](./DATA_MODELING.md) type map. A `str` field with `max_length=64` and an introspected `character varying(64)` canonicalize to the same `VARCHAR(64)` token and produce **no** operation. This prevents false-positive churn from spelling differences (`int8` vs `bigint`, `timestamptz` vs `timestamp with time zone`).

### 3.3 Algorithm (ordered)

The diff produces operations in a **dependency-safe order** so a single plan applies cleanly within one transaction:

```text
1. CreateTable        for tables in target ∧ not in current
2. AddColumn          for columns in target ∧ not in current        (nullable/default first)
3. AlterColumnType    for type mismatches                            (classify narrowing → destructive)
4. SetDefault/Drop    for default mismatches
5. AddConstraint      for constraints in target ∧ not in current     (after columns exist)
6. CreateIndex        for indexes in target ∧ not in current
7. DropIndex          for indexes in current ∧ not in target
8. DropConstraint     for constraints in current ∧ not in target
9. SetNotNull/Drop    nullability changes                            (SetNotNull on populated → destructive)
10. DropColumn        for columns in current ∧ not in target         (destructive)
11. DropTable         for tables in current ∧ not in target          (destructive)
```

Ordering rationale: **create before constrain, constrain before drop.** Additive structure lands first (so new constraints/indexes have their columns), then removals run last (so nothing depends on a dropped object mid-plan). This ordering also minimizes the window in which the schema is internally inconsistent (Blast Radius).

### 3.4 Destructive classification

Each operation is classified at plan time; the classification is what the dry-run labels and what the confirmation gate (§7) keys on. This satisfies the security gates MIG-1/MIG-2 and the acceptance criterion *"auto-detection covers add/remove/alter column and index changes"* with correct destructive tagging.

| Operation | Destructive? | Reason |
|-----------|--------------|--------|
| `CreateTable`, `AddColumn` (nullable / with default), `SetDefault`, `DropDefault`, `CreateIndex`, `DropIndex`, `DropNotNull`, `AddConstraint` (check/unique that current data satisfies) | **No** | Additive or reversible; no data loss |
| `DropColumn`, `DropTable` | **Yes** | Irreversible data loss |
| `AlterColumnType` — *widening* (`INTEGER→BIGINT`, `VARCHAR(64)→VARCHAR(128)`, `VARCHAR→TEXT`) | **No** | Lossless |
| `AlterColumnType` — *narrowing* (`BIGINT→INTEGER`, `TEXT→VARCHAR(n)`, `NUMERIC(10,2)→NUMERIC(6,2)`, any cross-type) | **Yes** | Truncation/overflow/corruption (MIG-2) |
| `SetNotNull` on a populated column with no safe default | **Yes** | Fails on existing NULLs / requires table rewrite |
| `AddColumn` `NOT NULL` with no default on a populated table | **Yes** | Same as above |
| `DropConstraint` | **No** (data-preserving) but **flagged** | Removes a guarantee; surfaced in dry-run for review even though no rows are lost |
| `AddConstraint` that **existing data violates** | **Yes** (apply will fail) | Validated against live state during dry-run; surfaced as a blocking finding |

**Narrowing detection** is computed from the §3.2 canonical types using a partial order over the v0.1 type map (numeric width, varchar length, precision/scale for `NUMERIC`). Any change that is not provably lossless is classified **narrowing → destructive**. There is no "probably fine" tier — uncertainty resolves to destructive (Defense in Depth, Fail Securely).

### 3.5 Drift, collisions, and managed scope

- **Drift** = the live DB state differs from what the ledger says was last applied (e.g. a manual `ALTER` outside Ferrum). The introspector computes a **state marker** (a hash of the canonical current schema for the managed scope); ADR-007's token binds to it so an apply built against a now-drifted database is rejected before mutation (§7.3).
- **Name collision** = a model maps to a table/index name that already exists but was not created by Ferrum (no ledger record, structural mismatch). The planner refuses to silently adopt or alter it; it surfaces a blocking finding telling the operator to rename or take ownership explicitly. (Never auto-`ALTER` an object Ferrum doesn't own — Blast Radius.)

### 3.6 Determinism guarantees

- Same `(target_state, current_state)` ⇒ byte-identical plan (canonical inputs + fixed operation ordering + deterministic names).
- Index/constraint names are deterministic ([DATA_MODELING.md §6.2](./DATA_MODELING.md)), so the same model yields the same DDL on every machine — required for stable diffs and for the plan digest the token binds to.
- The planner is stateless and side-effect-free (pure `diff`), so concurrent planning is safe and reproducible ([ARCHITECTURE.md §6.3](./ARCHITECTURE.md) immutable-metadata property).

---

## 4. Migration History Table (the ledger)

The ledger is the **persistent, append-only record of applied migrations**, owned by Python ([ARCHITECTURE.md §9.3, §10.2](./ARCHITECTURE.md)). It prevents double-apply, establishes apply order, supports squash equivalence, and anchors drift/state-marker computation.

### 4.1 Table design

```sql
CREATE TABLE ferrum_migrations (
    id              BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    name            TEXT        NOT NULL,            -- migration identifier, e.g. "0007_add_post_published_idx"
    branch_label    TEXT,                            -- optional named branch/app this migration belongs to (§8)
    plan_digest     TEXT        NOT NULL,            -- canonical digest of the operations in this migration
    applied_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    applied_by      TEXT,                            -- sanitized actor (username/CI id) — never a secret/DSN
    duration_ms     INTEGER,
    ferrum_version  TEXT        NOT NULL,            -- Ferrum version that applied it (forward-compat audit)
    replaces        TEXT[]      NOT NULL DEFAULT '{}', -- squash provenance: names this migration collapses
    status          TEXT        NOT NULL DEFAULT 'applied'  -- 'applied' | 'partial' (non-transactional failure, §6.4)
);

CREATE UNIQUE INDEX uq_ferrum_migrations_name ON ferrum_migrations (name);
```

| Column | Purpose | Lens |
|--------|---------|------|
| `name` (unique) | Identity; the unique index is what makes apply idempotent (re-applying a recorded name is a no-op) | Least Astonishment |
| `plan_digest` | Canonical digest of this migration's operations; powers squash equivalence checks and audit | Schema Evolution |
| `branch_label` | Distinguishes parallel branches so merge conflict detection (§8) can reason about lineage | (conflict detection) |
| `replaces` | Squash provenance (§2.3): apply is satisfied by this row **or** by all replaced names | Strangler Fig |
| `status` | `partial` marks a non-transactional step that failed mid-way (§6.4) so the next run knows recovery is needed | Blast Radius |
| `applied_by` | Sanitized audit only — **must never contain** a DSN, password, or token | Defense in Depth (CRED-1) |

### 4.2 Properties & invariants

- **Append-only on success.** A successful apply inserts one row per migration. Rows are not mutated except to flip `status` from `partial`→`applied` when a recovered non-transactional step completes (§6.4).
- **Created on first use.** If `ferrum_migrations` does not exist, the first `apply`/`dry-run` creates it (itself a transactional, additive operation). Its own creation is never gated as destructive.
- **The ledger holds no secrets.** No DSN, password, bound value, or row data. `applied_by` is an allowlisted sanitized actor string. This is enforced and scanned in release qualification (CRED-1, §9).
- **Optional apply lock.** For multi-process deploys, apply takes a PostgreSQL advisory lock (a single well-known lock key) for the duration of the apply so two instances cannot apply concurrently ([ARCHITECTURE.md §10.2](./ARCHITECTURE.md)). The lock is best-effort serialization, not a distributed transaction; it is released on commit/rollback/connection loss.
- **State marker derivation.** The drift/state marker (§3.5, §7) is derived from `(set of applied names + canonical current schema digest)`. The ledger is one of its two inputs; the live catalog is the other. Binding to both is what makes the token reject *both* "someone applied another migration since dry-run" and "someone hand-altered the schema since dry-run."

### 4.3 Why a DB table (not a file)

| Alternative | Why rejected |
|-------------|--------------|
| File-based ledger (e.g. JSON in repo) | The ledger must reflect the *target database's* applied state, which is per-environment and not a repo property. A prod and a staging DB have different applied sets; a file in the repo cannot represent that. (Data Gravity — apply state lives with the data.) |
| No ledger (re-diff every time) | Cannot distinguish "never applied" from "applied then drifted"; cannot support squash equivalence; cannot make apply idempotent. |
| External migration tool's table | Couples Ferrum to a third tool's schema; v0.1 owns a minimal table it fully controls (YAGNI + Blast Radius). |

---

## 5. Migration File Format

A migration is a **Python module** declaring an ordered list of operation objects plus dependency metadata. Operations are typed Python objects (Least Astonishment: reviewable, diffable, hand-editable), not opaque SQL blobs or JSON.

### 5.1 File shape

```python
# migrations/0007_add_post_published_index.py
from ferrum.migrations import Migration, operations as ops


class Migration0007(Migration):
    # Lineage: the migration(s) this one applies after. Drives ordering + merge detection (§8).
    dependencies = ["0006_create_post"]

    # Squash provenance (empty unless this file is a squash, §2.3).
    replaces = []

    # Ordered, dependency-safe operations (the diff emits this order, §3.3).
    operations = [
        ops.AddColumn(
            table="posts",
            column="published",
            pg_type="BOOLEAN",
            nullable=False,
            default="false",          # allowlisted literal default; makes NOT NULL add non-destructive
        ),
        ops.CreateIndex(
            table="posts",
            name="ix_posts_created_at_published",   # deterministic name (DATA_MODELING §6.2)
            columns=["created_at"],
            where="published = true",                # partial index predicate, allowlist-validated
            unique=False,
            concurrently=False,                      # transactional by default (ADR-004)
        ),
    ]
```

### 5.2 Operation object contract (v0.1)

Each operation is a frozen object carrying **(a)** enough metadata to emit forward DDL, **(b)** its destructive classification, **(c)** its transactionality, and **(d)** its inverse (for in-transaction rollback and future down-migrations). Identifiers in operations are validated against the model metadata allowlist at apply time; an operation naming an unknown table/column fails before any SQL runs (Defense in Depth, mirrors [DATA_MODELING.md DM-1](./DATA_MODELING.md)).

| Operation | Key fields | Forward DDL | Inverse (§6.3) | Destructive | Transactional |
|-----------|-----------|-------------|----------------|-------------|---------------|
| `CreateTable` | table, columns[], pk, constraints[] | `CREATE TABLE` | `DropTable` | No | Yes |
| `DropTable` | table, (captured shape for inverse) | `DROP TABLE` | `CreateTable` (structure only; **data not restorable**) | **Yes** | Yes |
| `AddColumn` | table, column, pg_type, nullable, default | `ALTER TABLE … ADD COLUMN` | `DropColumn` | No (Yes if NOT NULL + no default on populated) | Yes |
| `DropColumn` | table, column, (captured col meta) | `ALTER TABLE … DROP COLUMN` | `AddColumn` (structure only; **data not restorable**) | **Yes** | Yes |
| `AlterColumnType` | table, column, from_type, to_type, narrowing | `ALTER TABLE … ALTER COLUMN … TYPE … USING …` | `AlterColumnType` (to ← from) | Yes iff narrowing | Yes (rewrite may lock) |
| `SetNotNull` / `DropNotNull` | table, column | `ALTER COLUMN … SET/DROP NOT NULL` | the opposite op | `SetNotNull` on populated = Yes | Yes |
| `SetDefault` / `DropDefault` | table, column, expr | `ALTER COLUMN … SET/DROP DEFAULT` | the opposite op (prior expr captured) | No | Yes |
| `AddConstraint` | table, constraint meta (unique/check/fk) | `ALTER TABLE … ADD CONSTRAINT` | `DropConstraint` | No (Yes if data violates) | Yes |
| `DropConstraint` | table, name, (captured meta) | `ALTER TABLE … DROP CONSTRAINT` | `AddConstraint` | No (flagged) | Yes |
| `CreateIndex` | table, name, columns, where, unique, concurrently | `CREATE [UNIQUE] INDEX [CONCURRENTLY]` | `DropIndex` | No | Yes unless `concurrently=True` |
| `DropIndex` | name, (captured def) | `DROP INDEX [CONCURRENTLY]` | `CreateIndex` | No | Yes unless `concurrently=True` |

**Inverse-capture rule.** Operations that destroy structure (`DropColumn`, `DropTable`, `DropConstraint`, `DropIndex`, `AlterColumnType`) capture enough of the *prior* definition at generation time to emit a structural inverse. The inverse restores **structure, not data** — this is stated explicitly because it is a Least-Astonishment hazard: rolling back a `DropColumn` re-creates the column empty; it does not resurrect the dropped values. v0.1's real protection against data loss is the destructive gate (§7), not rollback.

### 5.3 SQL-safety of operations

- Operations never carry **raw user SQL**. `pg_type`, constraint expressions, partial-index predicates, and defaults are the same allowlisted forms validated at class-definition time ([DATA_MODELING.md §3.3, §5.2, §6.2](./DATA_MODELING.md)). A hand-edited migration that introduces an out-of-allowlist expression fails validation at apply, not silently at the database.
- Identifiers (table/column/index/constraint names) resolve from the metadata allowlist; they are never re-interpreted as runtime strings ([ARCHITECTURE.md Layer 2/3](./ARCHITECTURE.md)).
- This keeps the migration path under the **same no-raw-SQL invariant** as the query path. DDL is generated from typed operations, not assembled from strings.

### 5.4 File naming & ordering

- Files are `NNNN_short_slug.py` with a zero-padded monotonic sequence per branch. The numeric prefix is a human convenience; **true ordering is the `dependencies` graph**, not the filename (so merges that interleave numbers still order correctly — §8).
- `makemigrations` picks the next sequence and a slug derived from the dominant operation; both are overridable.

### 5.5 Reserved: data operations (v0.2 forward contract — not implemented)

> **Scope marker.** A `RunPython(forwards, backwards)` data-operation shape is **reserved** so v0.1's operation list does not foreclose data backfills later (Evolutionary Architecture). v0.1 ships **DDL operations only**. Engineers must not implement a data-migration runner in v0.1 (YAGNI; PRD scope). When it lands, it will be one more operation type in the same ordered list, executing inside the same transactional wrapper (or marked non-transactional when it must run in batches).

---

## 6. Forward & Rollback Execution Semantics

Execution is owned by the Python apply orchestrator; SQL text and the plan digest come from Rust ([ARCHITECTURE.md §8.6](./ARCHITECTURE.md)). The model is **per-migration transactional with a documented non-transactional exception list** ([ARCHITECTURE.md ADR-004](./ARCHITECTURE.md)).

### 6.1 Forward apply (transactional path — the common case)

```text
for each unapplied migration in dependency order:
  BEGIN
    for each operation in migration.operations:
        execute forward DDL
    INSERT INTO ferrum_migrations (...)        -- ledger row in the SAME transaction
  COMMIT                                         -- atomic: schema change + ledger row land together
  emit migration_apply_success (Tier A)
```

The ledger insert is **inside** the migration's transaction. This is the key Blast-Radius property: a migration and its ledger record commit or roll back **together**, so the ledger can never claim a migration applied that actually rolled back, and vice versa. This is what lets failure output truthfully state *"database unchanged"* for the transactional path (PRD MIG-4, L185/L193).

### 6.2 Forward failure (transactional path)

```text
operation N raises (e.g. AddConstraint violated by existing data)
  ROLLBACK                                       -- entire migration undone, including any earlier ops in it
  ledger unchanged                               -- no row inserted
  emit migration_apply_failure (Tier A)
  output: "Database unchanged. Migration 0007 failed at step N (AddConstraint ck_x).
           Recovery: fix the data or amend the migration, then re-run apply."
```

For transactional migrations, recovery is simply **re-run after fixing the cause** — the DB is byte-identical to its pre-attempt state. No manual cleanup, no partial ledger.

### 6.3 Rollback semantics (within v0.1 scope)

v0.1 has **two** distinct rollback meanings; conflating them is the trap:

1. **Transactional auto-rollback (shipped, primary).** PostgreSQL `ROLLBACK` undoes a failed transactional migration completely (§6.2). This is the real recovery mechanism for the common path. It needs no inverse operations — the database engine reverts.
2. **Operation inverse (defined, used by #1 and reserved for v0.2 down-migrations).** Every operation defines its structural inverse (§5.2). In v0.1 this inverse is what a future `migrate down` would emit and is documented per operation so the contract is complete — but **no standalone down-migration command ships in v0.1** ([ARCHITECTURE_FEASIBILITY_REVIEW.md §8](./ARCHITECTURE_FEASIBILITY_REVIEW.md): rollback deferred to forward-migrations + manual recovery). To undo a *committed* migration in v0.1, the operator writes a **forward fix-up migration** (e.g. a new migration that re-adds a column). This is deliberate: forward-only history is simpler to reason about and avoids the "down-migration that loses data on the way down" footgun.

**Why not ship down-migrations in v0.1:** down-migrations imply they restore prior state, but a down of `DropColumn` cannot restore data (§5.2). Shipping them would create a false safety signal (Least Astonishment violation). v0.1's honest contract: transactions protect you mid-migration; forward fix-ups + the destructive gate protect you across migrations.

### 6.4 Non-transactional operations (the ADR-004 exception list)

Some PostgreSQL operations **cannot** run inside a transaction block ([ARCHITECTURE.md ADR-004](./ARCHITECTURE.md)):

- `CREATE INDEX CONCURRENTLY` / `DROP INDEX CONCURRENTLY`
- Certain `ALTER TYPE` / enum-value operations
- Any operation PostgreSQL forbids in a transaction block

These are **marked non-transactional per step** at plan time and executed **outside** the wrapping transaction, one at a time:

```text
for each operation marked non_transactional:
  -- run OUTSIDE a transaction; cannot be rolled back by the engine
  pre-insert ledger row with status='partial' (its own small txn)   -- so a crash here is detectable
  execute operation (no surrounding BEGIN)
  on success: update ledger row status -> 'applied'
  on failure: ledger row stays 'partial'
              emit migration_apply_failure (Tier A)
              output: "Database MAY have changed. Migration 0008 step M
                       (CREATE INDEX CONCURRENTLY ix_...) failed and is NOT transactional.
                       Current state: index may exist as INVALID.
                       Recovery: DROP INDEX IF EXISTS ix_... then re-run apply (it will recreate),
                       or VALIDATE the index manually. See migration output for the exact name."
```

Properties:

- **Honest state reporting (MIG-4).** Because these can't roll back, the failure message states *"database MAY have changed"* and the exact recovery action — never a false *"unchanged."* The `status='partial'` ledger row is the durable breadcrumb the next run reads.
- **Idempotent recovery.** Recovery instructions use `IF EXISTS` / `IF NOT EXISTS` patterns so re-running apply after a partial failure converges to the intended state without manual surgery where possible.
- **Mixed plans split the transaction boundary.** A plan that contains both transactional and non-transactional operations is executed as: transactional ops in their `BEGIN…COMMIT`, then each non-transactional op standalone. The dry-run shows exactly where the transaction boundary breaks so reviewers see the non-atomic seam.

### 6.5 Mandatory dry-run before apply (MIG-1)

`apply` **requires** a dry-run to have produced the plan it is applying:

- `ferrum migrations dry-run --env <target> [--format json]` prints the ordered operations, each labeled with destructive/transactional flags, the SQL that will run, and the transaction-boundary map. It mutates nothing (PRD L274).
- For non-interactive/destructive/non-dev apply, the dry-run also emits the **confirmation token** (§7).
- Apply with no corresponding reviewed plan/token fails (MIG-1). There is no apply path that skips dry-run.

---

## 7. Confirmation Gates (consuming ADR-007)

This section specifies what the **planner** must produce so the [ARCHITECTURE.md ADR-007](./ARCHITECTURE.md) gate works; the gate's authorization policy lives in ADR-007 and is not re-litigated here.

### 7.1 Gate matrix

| Apply scenario | Required confirmation |
|----------------|------------------------|
| Dev target, non-destructive | none beyond running apply |
| Dev target, destructive (interactive) | interactive destructive confirmation |
| Non-dev target (any plan) | `--confirm-environment <target>` (dual gate even if non-destructive) |
| Non-interactive destructive **or** non-dev | `--confirm-plan <token>` **and** `--confirm-environment <target>` |

### 7.2 Plan digest the token binds to

The planner emits a **canonical plan digest**: a stable hash over the ordered, canonicalized operations (the same canonical form used for determinism, §3.6). ADR-007 binds the token over:

```text
token = HMAC-or-hash( canonical_plan_digest ‖ target_env ‖ sanitized_db_identity ‖ dry_run_state_marker )
```

- `canonical_plan_digest` — from this planner (§3.6). Identical models+DB ⇒ identical digest.
- `dry_run_state_marker` — the §4.2 state marker (applied-set + live-schema digest). Binding to it is what makes the token **stale after drift** (MIG-6).
- `sanitized_db_identity` — host/port/database/username only; **never** password or full DSN (CRED-1).

### 7.3 Failure modes (all fail before mutation)

Per ADR-007 and PRD L191, every one of these fails **before** any DDL runs, with re-run-dry-run guidance: missing token · malformed token · token for another target · token for another plan · token stale after schema drift (state marker mismatch). The single-use/ledger-bound marker (MIG-6) makes a replayed token fail after a successful apply because the state marker has advanced.

### 7.4 Anti-bypass (inherited, non-negotiable)

No `--force`, `--yes`, or env-var-only destructive bypass exists ([ARCHITECTURE.md ADR-007 anti-bypass invariant](./ARCHITECTURE.md)). The token may be *transported* via a secret channel (env/stdin) to avoid argv leakage (MIG-8), but the env var supplies the *dry-run-derived token*, never a bypass. Any apply path that mutates destructively without a matching live-state token is an architecture defect.

---

## 8. Conflict Detection (parallel branch merges)

Two engineers branching off the same migration head and each adding a migration is the classic ORM merge hazard: both files claim to apply after `0006`, producing **two heads**. Ferrum detects and refuses to silently apply an ambiguous history.

### 8.1 Migrations form a DAG, not a line

Ordering is the `dependencies` graph (§5.4), not filenames. A well-formed history has **exactly one leaf** (head). The detector walks the dependency graph of all migration files:

```text
build graph: node = migration, edge = dependency
heads = nodes with no successor (nothing depends on them)

if len(heads) == 1:  linear history — OK
if len(heads)  > 1:  CONFLICT — multiple migration heads detected
if cycle present:    CONFLICT — circular dependency (rejected, like the issue-blocker cycle rule)
```

### 8.2 Detection points

- **`makemigrations`** refuses to generate a new migration when multiple heads already exist — you must resolve the existing conflict first (Least Astonishment: don't pile a third head onto two).
- **`dry-run` / `apply`** refuse to run against a multi-head history. Apply against an ambiguous order is never allowed — it would make "what is the schema after apply?" non-deterministic (CAP framing: refuse rather than apply an ambiguous plan).
- **CI check** (recommended, documented): a `ferrum migrations check` command exits non-zero on multiple heads or cycles, so the conflict is caught at PR time, not deploy time (Observability First / shift-left).

### 8.3 Resolution: a merge migration

Conflicts are resolved by generating a **merge migration** that depends on **all** current heads, collapsing them back to one head:

```python
# migrations/0009_merge_0007_0008.py
class Migration0009Merge(Migration):
    dependencies = ["0007_add_published", "0008_add_author_idx"]  # both heads
    operations = []   # usually empty: a merge re-converges lineage; it adds no schema change itself
```

- `ferrum migrations merge` auto-generates this when the two branches touch **disjoint** schema objects (the common, safe case): the merge is a no-op re-convergence and the combined effect is order-independent.
- When the two branches touch the **same** object (e.g. both alter `posts.title`), the merge is **not** auto-resolvable. The planner surfaces the specific conflicting operations and requires the engineer to author the reconciling operations by hand. Ferrum never guesses which conflicting change wins (Least Astonishment + Blast Radius).

### 8.4 Why DAG + merge migration (not last-writer-wins or renumber-on-pull)

| Alternative | Why rejected |
|-------------|--------------|
| Last-writer-wins / auto-renumber on merge | Silently reorders schema history; two developers' changes can interleave into an order neither reviewed (Blast Radius). |
| Lock the migrations dir / serialize all schema work | Kills parallel development; not viable for a team product. |
| Detect only at apply time | Conflict surfaces at deploy, the worst moment. Detection at `makemigrations` + CI shifts it left. |

DAG-with-explicit-merge is the Django-proven, Least-Astonishment model: the conflict is **visible**, **reviewed**, and **resolved in a committed file**.

---

## 9. Security Requirements for Engineers (migration surface)

These map PRD/ARCHITECTURE/SECURITY gates to the migration layer. All are release-qualification gates and **must be test-covered**.

| ID | Requirement | Enforcement point | Test |
|----|-------------|-------------------|------|
| MIG-1 | Dry-run mandatory before apply | Apply orchestrator (§6.5) | Apply with no reviewed plan/token → fails before mutation |
| MIG-2 | Destructive ops (drop col/table, narrowing type, NOT-NULL-on-populated) gated; classified at plan time | Diff classifier (§3.4) | Destructive apply without confirm/token → fails; narrowing classified destructive |
| MIG-3 | Non-dev apply requires explicit `--confirm-environment` (dual gate) | Apply gate (§7.1) | Non-dev apply without env confirm → fails |
| MIG-4 | Failure output states whether DB changed, which step, recovery action; non-transactional steps report "may have changed" | Executor (§6.2, §6.4) | Inject failure on transactional vs non-transactional step → message asserts correct state + recovery |
| MIG-6 | Token unforgeable without live dry-run state; replay after successful apply fails | Plan digest + state marker (§7.2) | Replay token post-apply → fails (marker advanced) |
| MIG-7 | Token classified sensitive; dry-run/apply output secret-free | `migration_dry_run` hook + CLI docs (§7.4) | Dry-run JSON scan: token labeled sensitive; no DSN/password substring |
| MIG-8 | Token accepted via secret channel (env/stdin), not a bypass | Apply CLI (§7.4) | Token readable from stdin/env; no `--force`/`--yes`/env-only bypass exists |
| CRED-1 / CRED-3 | No DSN/password/bound value/row data in ledger, dry-run, or apply output | Ledger (§4.2) + dry-run emitter | Fixture-DSN scan over ledger rows + dry-run/apply output → no secret substring |
| MIG-LEDGER | Ledger commits atomically with its migration (transactional path) | Apply (§6.1) | Force failure mid-migration → no orphan ledger row; force success → exactly one row |

**SecurityEngineer notification.** The destructive classifier (§3.4), the plan digest / token binding the apply gate consumes (§7.2), the non-transactional partial-state reporting (§6.4), and the ledger secret-free guarantee (§4.2) require SecurityEngineer review at v0.1 release qualification. This is the **migration-apply threat-model deliverable** SecurityEngineer assigned to the Chief Architect ([SECURITY_REVIEW_PRD.md](./SECURITY_REVIEW_PRD.md): "Threat model SQL compilation + migration apply paths → Chief Architect → Architecture phase"); this document is its migration half.

---

## 10. Alternatives Considered

| Decision | Chosen | Alternative | Why rejected |
|----------|--------|-------------|--------------|
| Generation model | Auto-detect, commit, review, gated apply | Fully manual authoring | Loses Django-parity ergonomics; error-prone for routine additive changes |
| | | Auto-apply freshly-diffed plan at deploy | Defeats review gate; token can't bind to a *reviewed* plan (§2.1) |
| Migration artifact | Committed Python operation file | Generate-on-the-fly, never stored | No auditable history, no PR diff, no token-bindable reviewed plan |
| Operation representation | Typed Python objects | Raw SQL strings in files | Breaks no-raw-SQL invariant; not classifiable/invertible; injection surface |
| | | JSON/YAML op list | Less reviewable; loses Python editability for data ops later (§5.5) |
| Rollback model (v0.1) | Transactional auto-rollback + forward fix-ups | Ship `migrate down` down-migrations | Implies data restoration it can't deliver; false safety (§6.3) |
| History tracking | DB ledger table (`ferrum_migrations`) | File-based ledger in repo | Applied state is per-environment, not a repo property (§4.3) |
| Ledger atomicity | Ledger row inside migration txn | Ledger written after commit (separate step) | Crash between commit and ledger write → orphan state; can't truthfully report "applied" |
| Conflict model | Dependency DAG + explicit merge migration | Auto-renumber / last-writer-wins | Silently reorders reviewed history (Blast Radius) |
| Narrowing classification | Uncertainty ⇒ destructive | "Probably safe" heuristic tier | Fail-securely; a wrong "safe" guess corrupts data (§3.4) |
| Squash | Manual, opt-in, provenance-tracked | Automatic squash at threshold | Rewrites history under operators; YAGNI (§2.3) |

---

## 11. Open Items & Handoff

| Item | Owner | Blocks |
|------|-------|--------|
| Formal ADR records (ADR-004 transactionality, ADR-007 token) finalized in `DECISIONS.md` | ChiefArchitect ([GUY-74](/GUY/issues/GUY-74)) | ADR sign-off |
| QuerySet IR ↔ planner reuse of canonical metadata (shared canonicalization code path) | [QUERY_ENGINE.md](./QUERY_ENGINE.md) ([GUY-72](/GUY/issues/GUY-72)) | impl coordination |
| SecurityEngineer review of classifier + token digest + ledger secret-free + partial-state reporting (§9) | SecurityEngineer | release qualification |
| CEO/board approval of migration contract | CEO | migration implementation start |

### Acceptance criteria coverage (GUY-73)

- **Auto-detection covers add/remove/alter column and index changes** — §2.2 (detection table), §3.3 (algorithm), §3.4 (classification).
- **Rollback is always defined for each operation type** — §5.2 (inverse column for every operation), §6.3 (rollback semantics; transactional auto-rollback + per-op inverse + forward fix-up policy).
- **Migration history table design is specified** — §4 (`ferrum_migrations` DDL, columns, invariants, atomicity).
- **Conflict detection approach is documented** — §8 (DAG, multi-head detection, merge migration resolution).
- **Document committed to `/docs/foundation/MIGRATIONS.md`** — this file.

### Required-sections coverage (GUY-73)

- Migration philosophy (auto-detect vs manual, squashing) — §2.
- Schema diffing algorithm — §3.
- Migration file format (Python operations) — §5.
- Forward & rollback execution semantics — §6.
- Migration history tracking — §4.
- Conflict detection — §8.

### Engineer handoff

Implementation of the migration layer may begin once this document and `DECISIONS.md` are approved, and after the data-model layer ([DATA_MODELING.md](./DATA_MODELING.md)) lands — the planner's **target** input is `ModelMetadata`, so the metadata builder is the upstream dependency. Recommended slice order aligns with [ARCHITECTURE.md §17](./ARCHITECTURE.md): **(1)** introspector + canonical schema normalization, **(2)** pure diff/classifier with the §9 gates test-covered from the first slice, **(3)** file format + `makemigrations`, **(4)** ledger + transactional executor, **(5)** dry-run + ADR-007 token, **(6)** conflict detection + merge.

**Do not implement (architecture/scope guard):** standalone down-migration command, data-migration/backfill runner (§5.5), automatic squash triggers, any non-PostgreSQL dialect path, or any destructive apply path that bypasses the dry-run/token gate. These are deferred seams or banned bypasses, not v0.1 work.

---

*Produced by Chief Architect for [GUY-73](/GUY/issues/GUY-73). Pending CEO/board migration-contract approval.*
