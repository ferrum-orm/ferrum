# Ferrum Developer Experience & Design

This document specifies Ferrum's developer experience: the onboarding journey, documentation
information architecture, terminal-level CLI/UX mockups for error and migration workflows, the
tiered observability narrative, accessibility requirements, and testable UX acceptance criteria.
It complements [`./PRODUCT_REQUIREMENTS.md`](./PRODUCT_REQUIREMENTS.md) (the v0.1 product
contract), [`./ARCHITECTURE.md`](./ARCHITECTURE.md), and [`./SECURITY.md`](./SECURITY.md) (the
release-qualification security gates).

---

## 1. Design Vision

Ferrum v0.1 aims to feel less like a "programmer-default" library and more like a premium,
polished tool that is exceptionally easy to adopt and run.

Every developer touchpoint — from reading the first page of the documentation to diagnosing a
broken query in production — is evaluated against rigorous UX methodologies. This translates the
raw product requirements (with security gates integrated) into a cohesive developer experience:
it defines the onboarding journey, specifies terminal-level mockups for error and migration
workflows, and outlines the documentation hierarchy.

---

## 2. Developer Onboarding Narrative: The First-Run Path

The primary success metric for Ferrum v0.1 is that a developer can go from landing on the
documentation to executing their first successful async PostgreSQL query in **under 30 minutes**.

### 2.1 The Onboarding Stage Map
The first-run experience divides into sequential phases, optimizing for the **Goal-Gradient
Effect**: as the developer gets closer to running their query, momentum increases, provided
friction is minimized.

```
┌──────────────────┐     ┌──────────────────┐     ┌──────────────────┐     ┌──────────────────┐     ┌──────────────────┐
│ 0. Scaffold      │ ──> │ 1. Align Model   │ ──> │ 2. Get Connected │ ──> │ 3. Bootstrap DB  │ ──> │ 4. First Query   │
│  (ferrum init)   │     │  (Mental Models) │     │  (Frictionless)  │     │  (Safe Autom.)   │     │  (Instant Flow)  │
└──────────────────┘     └──────────────────┘     └──────────────────┘     └──────────────────┘     └──────────────────┘
```

#### Phase 0: Project Scaffolding (`ferrum init`)
* **Goal**: Create the minimum local project files for the documented quickstart without Ferrum owning container lifecycle.
* **UX Strategy**: **Defaults** + **Recognition over Recall** — one command writes a Ferrum config stub, `.env.example`, and a PostgreSQL `docker-compose.yml` with synthetic placeholders only.
* **Scope boundary**: `ferrum init` is in v0.1 scope. `ferrum dev-db` is **out of scope**; developers start PostgreSQL with standard `docker compose up -d postgres` documented in the quickstart.
* **Idempotency**: Re-running `ferrum init` in a directory with existing target files prints planned file actions and exits without overwriting by default. Overwrite requires an explicit documented flag for scaffolding only.
* **Lens:** **Tesler's Law** — Ferrum absorbs setup complexity in scaffolding; Docker lifecycle stays in familiar `docker compose` commands (**Jakob's Law**).

#### Phase 1: Model Definition & Mental Model Alignment
* **Goal**: Establish how Ferrum's Pydantic v2-native structure works.
* **UX Strategy**: Leverage **Jakob’s Law** (developers expect Ferrum to behave like Pydantic and Django). Present side-by-side or clean unified model declarations.
* **Cognitive Load Reduction**: Developers should not have to declare separate database schemas and Pydantic validation schemas. Emphasize the unified class model as the single source of truth.

#### Phase 2: Connection Setup & Credential Safety
* **Goal**: Define connection settings safely.
* **UX Strategy**: Prevent **Choice Overload** by providing a single, clear default pattern: using an environment variable (`FERRUM_DATABASE_URL`).
* **Design Pattern**: Out-of-the-box configuration reading. If `FERRUM_DATABASE_URL` is present, initialization is zero-boilerplate.

#### Phase 3: DB Schema Bootstrapping
* **Goal**: Generate and apply the initial schema.
* **UX Strategy**: The developer runs `ferrum migrations generate` and `ferrum migrations apply`.
* **Friction Elimination**: In development, auto-create the meta-tables needed to track migrations. Skip deep validation confirmation gates in local development environments to establish **Flow** and keep the **Doherty Threshold** under 400ms.

#### Phase 4: The "Aha!" Moment (First Query)
* **Goal**: Execute `await User.objects.create(...)` and print the output.
* **UX Strategy**: Provide a copy-pasteable script that is fully functional and uses the `docker-compose.yml` scaffolded by `ferrum init`.

#### Phase 5: Migration Preview & Safe Defaults (Should-Have)
* **Goal**: Run `ferrum migrations dry-run`, review destructive callouts, then apply in development.
* **UX Strategy**: **Progressive Disclosure** — quickstart links to migration preview only after first CRUD succeeds so cognitive load stays bounded (**Miller's Law**).
* **Handoff**: Quickstart §4 points to `/docs/migrations` and `/docs/observability` for tier defaults.

### 2.2 Onboarding Error & Empty States (Should-Have)
Common first-run failures need structured recovery, not raw tracebacks.

| Failure | Stable code | User-facing recovery |
|---------|-------------|----------------------|
| Missing `FERRUM_DATABASE_URL` | FERR-C001 | Show env-var setup snippet; link `/docs/quickstart#connection` |
| PostgreSQL unreachable | FERR-E101 | Host/port only; suggest `docker compose up -d postgres` (from scaffolded compose file) |
| `ferrum init` target files exist | FERR-I001 | List planned files; exit without overwrite; document `--overwrite` for scaffolding |
| Models not registered / no tables | FERR-M001 | Point to `ferrum migrations generate` + `apply` |
| Async loop misuse (sync call) | FERR-A001 | Show `await` fix with FastAPI lifespan example |
| Tier C inspector in CI | FERR-D001 | "Query inspection is local-dev only; use Tier A hooks in CI" |

**Lens:** Nielsen #9 — help users recognize, diagnose, and recover from errors. Empty states (no migrations yet, no models) use plain-language CTAs: "No migrations found — run `ferrum migrations generate` after defining models."

**Acceptance criteria:**
- Each quickstart step failure maps to one recovery block with doc deep link.
- Empty migration list shows generate CTA, not a blank screen.
- Setup errors never echo DSN or submitted credentials.

### 2.3 `ferrum init` CLI Output (Mockup)

Command: `ferrum init`

```text
✨ Ferrum project scaffold
================================

  Will create:
  ├─ ferrum.toml          (local config stub)
  ├─ .env.example         (FERRUM_DATABASE_URL placeholder — copy to .env)
  ├─ docker-compose.yml   (PostgreSQL for local development)
  └─ examples/quickstart.py

  Next steps:
  1. cp .env.example .env
  2. docker compose up -d postgres
  3. ferrum migrations generate && ferrum migrations apply
  4. python examples/quickstart.py

  Note: Ferrum scaffolds files only. Database lifecycle uses standard Docker Compose.
```

**Idempotent re-run** (existing files present):

```text
⚠️  Ferrum init — files already exist
================================

  Skipped (already present):
  ├─ ferrum.toml
  ├─ .env.example
  └─ docker-compose.yml

  No files were overwritten. Re-run with --overwrite to replace scaffold files.
```

**Acceptance criteria:**
- Init output lists every file action before write.
- Generated `.env.example` and `docker-compose.yml` use synthetic placeholders only.
- Quickstart explicitly documents `docker compose` — no `ferrum dev-db` command or alias.

---

## 3. Documentation Information Architecture (IA)

To maximize **Information Scent**, documentation must be structured to help developers locate critical concepts immediately without reading massive blocks of text. **Miller's Law** keeps the main navigation sections bounded to $7 \pm 2$ high-level nodes, and the **Inverted Pyramid** pattern governs content delivery.

```
/docs
├── 1. Positioning & Comparative Guides (Why Ferrum?)
├── 2. Quickstart & Installation (The 30-Min Promise)
├── 3. Core Concepts (Models, QuerySets, Fields)
├── 4. Schema Migrations (Generate, Dry-Run, Apply)
├── 5. Failure-Mode Encyclopedia (Stable Error Taxonomy)
└── 6. Observability & Safe Logs (Tiers, Hooks, Audit)
```

### 3.1 Document Mapping & Hierarchy Spec

1. `/docs/positioning`  
   * **Scope**: Comparisons vs. SQLAlchemy, Tortoise, and Django ORM.  
   * **UX Pattern**: Interactive comparison table. Clear differentiation highlights when to use Ferrum (and when *not* to use it).
2. `/docs/quickstart`  
   * **Scope**: A 5-step setup: `ferrum init` → copy `.env.example` → `docker compose up -d postgres` → migrations → first query.  
   * **UX Pattern**: Copy-paste blocks with active "Copy" actions. Plain Language descriptions of async runtime loop integration (FastAPI / Starlette). Explicit callout that Ferrum scaffolds files but does not wrap Docker lifecycle.
3. `/docs/core-concepts`  
   * **Scope**: Modeling guide, Pydantic configuration, supported scalar types, and chainable QuerySet methods.  
   * **UX Pattern**: Visual cheat sheet representing chainable QuerySet filters (e.g., `.filter()`, `.exclude()`, `.order_by()`).
4. `/docs/migrations`  
   * **Scope**: How schema diffing works, editing generated migration files, and safety controls.  
   * **UX Pattern**: Highlight boxes for destructive operations. Steps on dry-run output inspection.
5. `/docs/failures-and-errors`  
   * **Scope**: Complete dictionary of stable error codes, what triggers them, and how to recover.  
   * **UX Pattern**: Organized by stable categories matching the product taxonomy (Validation, Compilation, Connection, Execution, Migration).
6. `/docs/observability`  
   * **Scope**: Guide to configuring hooks, understanding the three observation tiers (A, B, C), and safe APM log sanitization.  
   * **UX Pattern**: "Do & Don't" security code-blocks detailing log ingestion configurations.

---

## 4. Developer-Facing UX and Interaction Specs

A terminal is a user interface. For a CLI tool, terminal outputs and interactions are the primary touchpoints. Ferrum rejects "programmer defaults" (raw Python stack traces, unaligned text blocks) and specifies exactly how output should be formatted.

### 4.1 Error Readability and Sanitization UX
When an error occurs, the output must be immediately readable, identify the root cause without force-recalling internals, and be highly structured.

#### Lenses Applied:
* **Forms & Errors**: Forgiveness—provide clear, actionable recovery directions.
* **Gestalt - Proximity & Common Region**: Visually group related error context together so it reads as a single conceptual unit.
* **Accessibility**: Use high-contrast ANSI colors and distinct icons so the error is understandable even for developers with red-green color-blindness.

#### Local Dev Error View (Mockup)
```text
┌────────────────────────────────────────────────────────────────────────────┐
│ 🛑 Ferrum QueryCompilationError [FERR-C102]                                │
├────────────────────────────────────────────────────────────────────────────┤
│                                                                            │
│  What happened:                                                            │
│  The QuerySet filters contains an unknown field or operator.               │
│                                                                            │
│  Context:                                                                  │
│  • Model:      User                                                        │
│  • QuerySet:   User.objects.filter(emall__contains="@gmail.com")           │
│  • Problem:    Field "emall" does not exist.                               │
│                                                                            │
│  Did you mean:                                                             │
│  👉 email                                                                  │
│                                                                            │
│  How to fix:                                                               │
│  Update the filter argument on line 12 of "app/routes/users.py" to match   │
│  the correct model schema field definition.                               │
│                                                                            │
└────────────────────────────────────────────────────────────────────────────┘
```

#### Validation Error View (No Submitted Value Echo — LOG-2)
Validation failures must identify the offending field and constraint without echoing submitted values (passwords, tokens, PII). This applies to both CLI and in-app error surfaces.

```text
┌────────────────────────────────────────────────────────────────────────────┐
│ 🛑 Ferrum ValidationError [FERR-V101]                                      │
├────────────────────────────────────────────────────────────────────────────┤
│                                                                            │
│  What happened:                                                            │
│  Model validation failed before SQL was generated.                         │
│                                                                            │
│  Context:                                                                  │
│  • Model:      User                                                        │
│  • Field:      email                                                       │
│  • Constraint: value must be a valid email address                         │
│                                                                            │
│  How to fix:                                                               │
│  Correct the field value in your request payload or model instance.        │
│  (Submitted value omitted from default output.)                            │
│                                                                            │
└────────────────────────────────────────────────────────────────────────────┘
```

* **Lens:** Data minimization — default errors satisfy LOG-2; verbose value echo requires explicit Tier C / debug opt-in only.

#### Production Default Error View (Sanitized)
In production, to prevent data disclosure, Ferrum maps raw database error hints to stable sanitized models. No raw input values or schema internal identifiers are printed:

```text
2026-06-13 10:50:00 [ERROR] [FERR-E201] Database constraint violation: Unique constraint failed. Operation aborted safely. (Details redacted in production. Enable dev-debug configurations to view local trace details.)
```

---

### 4.2 Developer-Local Query Inspection UX (Tier C)
To comply with **DBG-1 (local-dev-only query inspection)**, Ferrum provides an explicit, opt-in local terminal helper. This tool is **strictly local** and cannot be served over remote HTTP routes.

#### Lenses Applied:
* **System & Interaction**: Recognition over Recall—clearly separate the parameterized query template from the bound values.
* **Accessibility**: Screen-reader and plain-text compliant headers.

#### CLI Inspection View (Opt-in Local Only)
Run via shell: `ferrum inspect-query "app.queries.get_active_users"`

```text
================================────────────────────────────────==============
                      FERRUM LOCAL QUERY INSPECTOR (Tier C)
================================================────────────────==============
[File]       app/queries.py
[Line]       24
[QuerySet]   User.objects.filter(is_active=True, role="developer").limit(5)

--- Generated Parameterized SQL (Tier B) -------------------------------------
SELECT id, email, is_active, role 
  FROM users 
 WHERE is_active = $1 
   AND role = $2 
 LIMIT 5;

--- Bound Parameters (Tier C - Unsafe for Production APM) ---------------------
• $1 (bool)  =>  true
• $2 (str)   =>  "developer"

--- Execution Diagnostics ----------------------------------------------------
• Expected Index Scan on idx_users_is_active_role (Cost: 0.15..8.30)
• Hydration Footprint: 4 fields per record into Pydantic model (User)
================================================────────────────==============
```

#### Query Inspection Copy/Export Constraints (DBG-1, LOG-3)
Tier C output is **local stdout only** — no clipboard auto-copy, no shareable URL, no HTTP export endpoint.

| Surface | Tier A (default) | Tier B (dev opt-in) | Tier C (local unsafe) |
|---------|------------------|---------------------|------------------------|
| CLI stdout | Metadata summary | Parameterized SQL | SQL + bound values |
| Clipboard / export | Blocked | Parameterized SQL only; values redacted | **Blocked by default**; explicit `--copy-sql` copies parameterized SQL only; `--copy-values` requires second confirmation prompt |
| APM / hooks | Allowed (safe) | Dev/staging opt-in | **Never** |
| Docs examples | Synthetic data only | Placeholders (`$1`, `$2`) | Labeled "local-only unsafe" with security callout |

**Lens:** Recognition over Recall — developers see tier badges in the inspector header (`[Tier C — LOCAL ONLY — NOT FOR LOGS]`). **Ethics:** no dark-pattern "share query" affordance that could leak bind values to Slack or ticket systems.

**Acceptance criteria:**
- Default inspector output cannot be copied with bound values without typing `yes` at a secondary prompt.
- Export to file writes to cwd only; filename includes `ferrum-local-inspect-` prefix and a one-line security header comment.
- Tier C sessions time out after terminal close; no persisted inspection cache.

---

### 4.3 Migration Dry-Run and Preview Experience
Migrations can be terrifying for developers. To establish trust, the migration command suite must make planned database transformations fully visible and explicitly call out destructive actions before they execute.

#### Lenses Applied:
* **Behavioral Science**: Loss Aversion—make data-loss risks prominent (**Von Restorff effect**).
* **Usability Heuristics**: Nielsen's #5 (Error Prevention) & Norman's Constraints—force explicit interaction to bypass destructive actions.
* **Accessibility**: Do not rely on color alone (e.g., red vs. green) to indicate danger; use textual callouts, borders, and warning icons.

#### Migration Dry-Run CLI Output (Mockup)
Command: `ferrum migrations dry-run`

```text
🔄 Planned Schema Migrations: 0003_add_user_profile.py
================================────────────────────────────────==============

  Non-Destructive Operations:
  ├─ [CREATE]  Table "profiles"
  ├─ [ADD]     Column "profile_id" to Table "users"
  └─ [INDEX]   Create index "idx_profiles_user_id" on "profiles" (user_id)

  ⚠️  DESTRUCTIVE OPERATIONS DETECTED (MIG-1, MIG-2):
  ┌────────────────────────────────────────────────────────────────────────┐
  │  [DROP]   Column "bio" from Table "users"                              │
  │           💥 WARNING: This will permanently delete all data stored     │
  │           in the "bio" column across all users. This cannot be undone! │
  │                                                                        │
  │  [ALTER]  Column "phone" on Table "users" (VARCHAR -> VARCHAR(15))     │
  │           💥 WARNING: Narrowing field length will truncate any existing│
  │           phone values longer than 15 characters!                      │
  └────────────────────────────────────────────────────────────────────────┘

==============================================================================
To safely apply these changes to target: DEVELOPMENT, run:
  ferrum migrations apply

To apply to non-development targets, explicit confirmation is mandatory.
```

#### Destructive Migration Confirmation Flow
When applying a migration containing destructive operations on non-development targets (e.g., STAGING, PRODUCTION):

```text
$ ferrum migrations apply --env production

🛑 WARNING: Migrating against non-development environment: "production" (MIG-3)
⚠️  This migration contains 2 DESTRUCTIVE operations that will result in data loss!

[!] To confirm you understand and accept the risk of permanent data loss,
    please type the target database name "production_ferrum_db":
    
    Enter database name: _
```

* **Anti-Pattern Refused**: Generic "y/n" prompts for destructive actions in production are not used. Forcing the user to type the database name leverages **Commitment & Consistency** to prevent accidental confirmation.

#### Non-Interactive CI/CD Confirmation Flow
Headless CI/CD destructive apply is **in v0.1 scope**, but only through a dry-run-scoped confirmation token — never `--force`, `--yes`, or env-var-only bypass.

**Step 1 — Dry-run emits token** (`ferrum migrations dry-run --env staging --format json`):

```text
🔄 Planned Schema Migrations: 0003_add_user_profile.py
Target: STAGING

  ⚠️  DESTRUCTIVE OPERATIONS DETECTED (2)

  Non-interactive apply requires a confirmation token from this exact dry-run.
  Token expires when the migration plan or target identity changes.

  confirmation_token: "ferr_mig_7kQ2…"   # opaque; bound to plan + env + sanitized db identity
```

**Step 2 — CI apply with token** (`ferrum migrations apply --env staging --non-interactive --confirm-plan <token> --confirm-environment staging`):

```text
✅ Ferrum MigrationApply
Target: STAGING (confirmed)
Plan token: valid (matches current migration plan)

Applying 4 operations…
```

**Token failure** (stale plan, wrong target, or missing token):

```text
🛑 Ferrum MigrationError [FERR-M302]
Non-interactive destructive apply rejected.

  Reason: Confirmation token does not match the current migration plan for target "staging".

  Recovery:
  1. Re-run: ferrum migrations dry-run --env staging --format json
  2. Review destructive operations in CI logs
  3. Apply with the new token from that dry-run output

  No database changes were made.
```

* **Lens:** **Norman's Constraints** — the token structurally binds review to apply; **Ethics** — Ferrum refuses a generic `--force` that teams would normalize into production runbooks.
* **Anti-Patterns Refused**: `--force`, `--yes`, lone `CONFIRM_DESTRUCTIVE=1` env vars, and clipboard auto-copy of tokens into shared CI logs (token appears in dry-run JSON only; apply step references it via secret/env injection, not echoed in success output).

#### Environment Awareness Badges
Dry-run and apply headers always show target context without secrets:

```text
Target:  STAGING
Database: ferrum_staging (host: db.internal.example, port: 5432)
User:     ferrum_app
Password: [REDACTED]
```

**Lens:** Information Scent — environment name is the first line; **Gestalt Proximity** groups connection metadata separately from operation lists.

#### Migration Apply Failure & Recovery Messaging
v0.1 does not ship automated rollback. Failure output must state **whether the database changed**, **which step failed**, and **documented recovery action** (forward-fix migration or manual intervention).

```text
🛑 Ferrum MigrationError [FERR-M401]
Migration apply failed at step 2 of 4.

  Migration:   0003_add_user_profile.py
  Environment: production
  Step failed: [ADD] Column "profile_id" to Table "users"

  Database state:
  • Steps 1 (CREATE TABLE "profiles") — APPLIED
  • Step 2 (ADD COLUMN) — FAILED
  • Steps 3–4 — NOT APPLIED

  Error category: Execution — duplicate column name

  Recovery (choose one):
  1. Forward fix: generate a corrective migration after inspecting schema drift.
  2. Manual: follow /docs/migrations#partial-failure-recovery for DBA checklist.

  Next command:
    ferrum migrations status   # inspect applied vs pending
    ferrum migrations dry-run  # re-preview before retry

  (No automatic rollback in v0.1. Do not re-run apply blindly.)
```

**Lens:** Forgiveness — failure is recoverable when the message is explicit; **Peak-End Rule** — end with actionable next commands, not a raw stack trace.

**Acceptance criteria:**
- Partial failure always prints APPLIED / FAILED / NOT APPLIED per step.
- Recovery section links to `/docs/migrations#partial-failure-recovery`.
- No password, DSN, or row data in failure output.

> **Migration atomicity note.** The common path uses transactional DDL (`BEGIN…COMMIT`) for
> PostgreSQL, so a failed step rolls back cleanly. A small set of non-transactional DDL
> statements are exceptions (enumerated in ADR-004); those get per-step-type recovery text since
> they can leave the database partially modified. The partial-failure mockup above is valid for
> the common transactional path.

---

### 4.4 Unscoped Bulk Mutation Danger API (MIG-5)
Unfiltered `delete()` and `update()` calls fail by default. Developers must use an explicit, named danger API for all-record mutations.

#### Lenses Applied
* **Norman's Constraints** — the safe path is the default; mass deletion requires deliberate opt-in.
* **Error Prevention (Nielsen #5)** — fail before SQL emission when filters are absent.

#### Default Failure (Unscoped Terminal Call)
```text
$ ferrum shell  # or in-app: await User.objects.delete()

🛑 Ferrum UnsafeOperationError [FERR-U301]
Unscoped bulk mutation blocked (MIG-5).

  • Operation: delete()
  • Model:     User
  • Problem:   No filters applied — this would affect ALL rows.

  To proceed intentionally, use the documented danger API:
    await User.objects.danger_delete_all()

  See: /docs/core-concepts#danger-apis
```

#### Danger API Naming & Documentation Requirements
* **API surface:** `danger_delete_all()` and `danger_update_all(**fields)` — names must signal irreversibility (no `.delete_all()` without `danger_` prefix).
* **Docs:** `/docs/core-concepts` includes a dedicated "Danger APIs" subsection with loss-aversion framing and examples showing filter-first alternatives.
* **Non-interactive CI:** Danger APIs are code-level only; they do not replace migration-style interactive confirmation. Application teams own authorization before calling danger APIs.

---

## 5. Observability Narrative & Diagnostics Tiers

To build high **Emotional Trust** with platform teams, Ferrum divides database logging into three rigorous, security-bounded tiers. This prevents sensitive data leakage (PII, credentials, access tokens) from reaching centralized logging systems (e.g., Elastic Observability, Datadog) while maintaining high local debuggability.

```
                  ┌─────────────────────────────────────┐
                  │ Tier A: Default Production-Safe APM │
                  │ Fingerprint, Duration, Outcome      │
                  └──────────────────┬──────────────────┘
                                     │
                     If staging/dev  ▼
                  ┌─────────────────────────────────────┐
                  │ Tier B: Parameterized SQL (Dev/Stg) │
                  │ SQL text with placeholders intact   │
                  └──────────────────┬──────────────────┘
                                     │
                     If local-only   ▼
                  ┌─────────────────────────────────────┐
                  │ Tier C: Full Local-Dev (Unsafe Log) │
                  │ Parameterized SQL + Bound Values    │
                  └─────────────────────────────────────┘
```

### 5.1 Data Boundary Specifications

* **Tier A (Default)**: Emits metadata only. Safe for APM, logs, and trace exports.
  * *Emitted Schema*: `{"fingerprint": "SELECT-users-12", "duration_ms": 1.2, "status": "success", "rows_hydrated": 1}`
  * *Secrets Exposure*: **0%**.
* **Tier B (Opt-in Staging)**: Emits normalized query strings without variable parameters.
  * *Emitted Schema*: `{"sql": "SELECT id FROM users WHERE status = $1", "duration_ms": 1.4, "status": "success"}`
  * *Secrets Exposure*: **0%**. Parameters are represented by placeholders (`$1`).
* **Tier C (Local Unsafe)**: Local execution debugging only. Banned from being served via endpoints.
  * *Emitted Schema*: Emits SQL and bound values `[true, "developer"]`.
  * *Secrets Exposure*: **High**. This must be heavily restricted to local standard output stream outputs.

---

## 6. Resolved Technical Decisions

The following architectural decisions are settled (see [`./ARCHITECTURE.md`](./ARCHITECTURE.md))
and confirm that the CLI mockups and latency assumptions in this document remain valid. No design
rework is required.

| Area | Decision | UX impact |
|------|----------|-----------|
| PyO3 exceptions / GIL / latency | Rust compile is **synchronous and GIL-holding**; a structured `Result` maps to a typed Python exception with no trace blobs; a panic surfaces as a catchable Python exception | §4.1 error boxes stay as specified; sub-millisecond compile keeps the **Doherty Threshold** comfortable |
| Migration atomicity | **Transactional DDL** (`BEGIN…COMMIT`) for the common path; non-transactional exceptions enumerated in ADR-004 | §4.3 partial-failure mockup is valid for the common path; ADR-004 exceptions get recovery text per step type |
| Hydration path | **Rust constructs the payload**; Pydantic v2 **construct-without-revalidate** for trusted DB rows (ADR-003) | Performance target met; `/docs/core-concepts` must document that custom validators are skipped on hydration by default (opt-in full validation) |

**Docs follow-up:** When `/docs/core-concepts` is authored, include an ADR-003 callout under
hydration semantics so developers with custom validators are not surprised (**Least Astonishment**).

---

## 7. Accessibility (A11y) Requirements for Ferrum Surfaces

Accessibility is not limited to graphical web pages. CLI systems and documentation sites must treat accessibility as a first-class citizen under **WCAG POUR**.

### 7.1 CLI and Terminal Accessibility Rules
1. **Color Independence**:
   * *Rule*: Output meaning must never be conveyed solely by color (WCAG Guideline 1.4.1). 
   * *Implementation*: Error boxes must use explicit structural text markings (e.g., `🛑`, `⚠️`, `[ERROR]`, `[WARNING]`) alongside color changes.
2. **Contrast Ratio**:
   * *Rule*: High-contrast ANSI colors only.
   * *Implementation*: Avoid dark-blue or low-contrast gray text on standard black terminal backgrounds. Use clean light-gray, white, yellow, or bold cyan styling for metadata.
3. **Screen Reader Friendliness**:
   * *Rule*: Avoid complex, multi-layered vertical ASCII tables that break linear screen reading.
   * *Implementation*: Keep terminal tables simple, linear, and use semantic plain-text column headers.

### 7.2 Documentation Accessibility Rules
1. **Semantic HTML**:
   * *Rule*: All documentation sites must use proper header semantics (`<h1>`, `<h2>`, `<h3>` nested sequentially).
2. **Keyboard Traversal & Focus Indicators**:
   * *Rule*: Any interactive component (e.g., copy buttons, tab switchers for comparisons) must be fully navigable via keyboard (`Tab` / `Space` / `Enter`) and show a clear, high-contrast focus outline.
3. **Alternative Text**:
   * *Rule*: Architectural diagrams (such as the Python/Rust boundary diagram) must include descriptive alt text or markdown text equivalents.

---

## 8. UX Acceptance Criteria

Testable criteria for QA and implementation verification:

### Onboarding & Documentation
- [ ] Quickstart completes in ≤5 labeled steps: `ferrum init` → env copy → `docker compose up -d postgres` → first query → migration preview pointer.
- [ ] `ferrum init` creates scaffold files; re-run without `--overwrite` exits without clobbering existing files.
- [ ] Scaffolded `.env.example` and `docker-compose.yml` contain placeholders only — no real credentials or production hostnames.
- [ ] Quickstart documents `docker compose` explicitly; no `ferrum dev-db` command or documentation reference.
- [ ] Each quickstart failure in §2.2 shows stable code, recovery text, and doc deep link.
- [ ] Main docs nav has ≤7 top-level sections (Miller's Law).
- [ ] Copy buttons on docs are keyboard-focusable with visible focus ring.

### Query Inspection
- [ ] Default production hooks emit Tier A only (no bound values, no DSN).
- [ ] Tier C inspector runs only in local context; no HTTP route exists.
- [ ] Tier C bound values cannot export without secondary confirmation.
- [ ] Inspector header includes textual tier badge (not color-only).

### Migration Preview & Safety
- [ ] Dry-run lists destructive ops in bordered region with `⚠️` + text warning (WCAG 1.4.1).
- [ ] Non-dev interactive apply requires typing database name (not y/n).
- [ ] Non-interactive destructive apply requires `--confirm-plan <token>` from the exact dry-run plus `--confirm-environment <target>`; succeeds only when token matches current plan and target.
- [ ] Stale, missing, or mismatched tokens fail before database mutation with actionable recovery text (re-run dry-run).
- [ ] No v0.1 path accepts `--force`, `--yes`, or env-var-only destructive confirmation.
- [ ] Partial failure output states per-step APPLIED/FAILED/NOT APPLIED.
- [ ] Environment header shows host/db/user; password always `[REDACTED]`.

### Errors & Accessibility
- [ ] Validation errors omit submitted values by default (LOG-2).
- [ ] Error boxes use icon + label + structure (not color alone).
- [ ] Unscoped bulk delete/update fails with danger API pointer (MIG-5).
