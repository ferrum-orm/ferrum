# Wave 1 DX Review — Ferrum v0.1

**Verdict:** Needs UX Adjustments  
**Reviewer:** product-designer (Wave 1 Track D)  
**Date:** 2026-06-13

Security foundation is solid (danger API guards, credential redaction, Tier A hook ordering, immutable chaining). Seven High-priority blockers must close before Wave 2 ships.

---

## High-Priority Blockers (must fix before Wave 2)

| ID | Issue | File |
|----|-------|------|
| B-1 | `User.objects` manager doesn't exist on `Model` — README's #1 entry point raises `AttributeError` | `models.py` |
| B-2 | `ferrum.ModelConfig` not exported from public namespace — referenced in docstring, missing from `__init__.py` | `__init__.py` |
| B-3 | `_build_ir()` absent from `QuerySet` — no Python IR serialization, no field-name validation, Rust gets nothing | `queryset.py` |
| B-4 | README has no quickstart setup steps — no install/env/init context; a new user cannot reach a working query | `README.md` |
| B-5 | `FERRUM_DATABASE_URL` env-var auto-detection unimplemented — PRODUCT_DESIGN.md §2.2 requires zero-boilerplate path | `connection.py` |
| B-6 | No stable error codes (`FERR-XXXX`) on any exception class — docs links and Failure-Mode Encyclopedia can't work | `errors.py` |
| B-7 | README roadmap puts migrations in v0.3, but PRD Must-haves require them in v0.1 — direct conflict | `README.md` |

---

## Section Reviews

### 1. Model definition ergonomics

**Issue:** README implies auto-table-naming from class name; docstring requires explicit `ModelConfig(table=...)`. These conflict.

**Proposal (High):** Auto-name from lowercase class name; `ModelConfig` is an override, not required. Align README and `models.py` docstring.

**Issue (Medium):** No `objects` class-level manager attribute — `User.objects.filter(...)` would raise `AttributeError`.

**Proposal (High):** Add a `Manager` descriptor on `Model` that returns a `QuerySet` bound to the model class.

### 2. QuerySet chaining ergonomics

**Issue (High):** README shows `filter(email__contains="@gmail.com")` double-underscore syntax — not implemented. Must either implement for v0.1 or correct README to equality-only.

**Issue (Medium):** `exclude()` appears in PRODUCT_DESIGN.md cheat sheet but is not stubbed.

**Issue (Low):** No `.values()` or `.values_list()` — acceptable v0.1 omission, document explicitly.

**Proposal:** For v0.1, support `filter(field=value)` (exact equality) and document other operators as v0.2.

### 3. Error message shapes

**Proposal:** Add stable `FERR-XXXX` codes to all exception classes:

- Unknown field in filter: `FERR-C102`  
  `FerrumCompileError: Unknown field 'emayl' on model 'User'. Available fields: id, email, is_active. [FERR-C102]`

- Unscoped delete/update (danger API): `FERR-U301`  
  `FerrumDangerApiError: delete() on User without a filter deletes all rows. Use danger_delete_all() to confirm. [FERR-U301]`

- Connection failure (redacted): `FERR-E101`  
  `FerrumConnectionError: Cannot connect to PostgreSQL at host=db.example.com port=5432 database=mydb username=app (category=connection_refused). [FERR-E101]`

**Proposal (Medium):** `FerrumCompileError` should carry `available_fields: list[str]` from `ModelMetadata` to power "Did you mean?" hints. Field names from allowlist only — never raw user input.

### 4. Quickstart narrative

**Issue (High):** README quickstart code block has no preceding setup steps. PRODUCT_DESIGN.md §2.1–§2.5 defines a 5-phase onboarding path (`pip install`, `ferrum init`, configure env, run migrations, first query) — none of this is in README.

**Proposal:** Add a minimal 5-step "Getting Started" section to README before Wave 2 ships the connection layer.

**Issue (High):** `FERRUM_DATABASE_URL` auto-detection (PRODUCT_DESIGN.md §2.2) is unimplemented. `connect()` currently requires explicit DSN argument.

### 5. ADR-002 IR ergonomics

**Issue:** `_build_ir()` is absent. No DX leakage risk (IR is correctly internal), but the validation-timing decision must be made.

**Recommendation:** Python pre-validates field names against `ModelMetadata` at `_build_ir()` time. This gives earlier, better-contexted errors ("Unknown field 'emayl' on User") vs. Rust-only validation (delayed, less context). Both are architecturally valid per ADR-006.

---

## Medium-Priority (address in Wave 2-3)

- `exclude()` stub
- `__contains` / `__gt` / `__lt` operator mapping (or explicit v0.1 doc note)
- `ferrum init` compose template test (INIT-1/2 — covered in Wave 4)
- README migration section aligned with PRD (v0.1 scope)

## Low-Priority (v0.2+)

- `.values()` / `.values_list()`
- `select_related()` / prefetch
- Sync compatibility (explicitly out of scope)
