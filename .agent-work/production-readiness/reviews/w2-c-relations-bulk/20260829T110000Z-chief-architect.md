---
task_id: w2-c-relations-bulk
run_id: 20260829T110000Z
authority: ChiefArchitect
reviewer: chief-architect-agent
reviewed_at: 2026-08-29T11:30:00Z
base_revision: 22931420c7c7212fe4e9718faa710d9a890ea473
decision: approved
scope:
  - python/ferrum/relations.py
  - tests/python/unit/test_relations.py
  - tests/python/unit/test_bulk_operations.py
  - tests/python/integration/test_relations_bulk.py
---

# Named Authority Verdict

## Authority

ChiefArchitect

## Claims reviewed

- Reverse FK/OTO/M2M loading architecture satisfies the task acceptance
  criteria and the ratified W0-A §5a contract (no identity map, no implicit
  lazy I/O, no unrestricted SQL).
- Nested `prefetch_related("posts__tags")` architecture is sound: through
  models, ordering, and bounded batching.
- Relation access stays explicit per §5a — forward M2M now raises, reverse
  FK/OTO return an unbound QuerySet with no I/O, reverse M2M raises.
- Bulk hardening architecture: composite keys, batch sizing, PostgreSQL
  parameter limits.
- Cascade behavior documented as database-driven; no SQLAlchemy-style
  unit-of-work cascade emulation.
- Corrective fix run (20260829T110000Z) resolved the 3 verification defects:
  false composite-PK docstring, misleading M2M ordering docstring, and dead
  code (`_model_pk_fields` / `_instance_pk_values`).
- No new IR nodes; `queryset.py` / `models.py` NOT modified (import only).

## Evidence

### Owned paths and diff scope

```
$ git diff HEAD --stat -- python/ferrum/queryset.py python/ferrum/models.py
  python/ferrum/connection.py python/ferrum/runtime.py python/ferrum/__init__.py
(no output — confirmed: W1-A/W2-B/W2-A/W1-B/E-owned files NOT modified)
```

`git diff HEAD -- python/ferrum/relations.py` — 382-line extension of the
pre-existing 421-line module to 683 lines (post-corrective-fix). No deletions
of public API; only additive helpers and docstring corrections.

### Reverse FK/OTO/M2M loading architecture

`python/ferrum/relations.py:545-593` (`_prefetch_reverse_fk`):
- Bounded batching via `_chunk_ids(parent_ids)` looping over
  `_MAX_PREFETCH_PARAMS` (32768) batches (line 574).
- `ORDER BY {pk_column}` (line 576) for deterministic reverse-FK output.
- Reverse OTO branch (line 585-589) stores `vals[0] if vals else None` —
  single object or None, not a list. Matches §5a "Reverse FK/OTO may return
  an unbound QuerySet filtered by FK".
- All identifiers quoted via `_quote_identifier` from
  `related_meta.table_name` / `rev.fk_column` / `related_pk.column_name`
  (lines 569-571) — immutable model metadata, never user input.

`python/ferrum/relations.py:595-683` (`_prefetch_m2m`):
- Two-phase architecture: Phase 1 through-table links (lines 637-653),
  Phase 2 target rows (lines 660-675).
- Both phases chunked via `_chunk_ids` (lines 640, 664).
- Through-table columns resolved via `_resolve_through_columns` (line 629)
  from `metadata.table_name` and `target_meta.table_name` — metadata-driven,
  no hardcoded convention.

### Nested prefetch architecture

`python/ferrum/relations.py:429-511` (`prefetch_related_objects`):
- `_split_nested_prefetch` (line 380-395) splits `"posts__tags"` into
  `("posts", "tags")`.
- First-level grouping by `first_levels` dict (lines 473-476) avoids loading
  the same relation twice when nested paths share a prefix (verified by
  `test_nested_prefetch_shared_prefix`).
- Recursive call at line 510 on freshly loaded related objects.
- `_collect_loaded_related` (line 513-535) handles list (reverse FK/M2M)
  and single-object (reverse OTO) cache shapes.
- `_related_model_for` (line 538-542) resolves the model class for the next
  level from either `ReverseRelationMeta` or `RelationMeta`.

This is a clean recursive architecture with no hidden I/O — prefetch only
runs after an explicit terminal (`all(conn)` / `first(conn)`) per the
`prefetch_related_objects` docstring (lines 457-460).

### §5a Explicit-access contract (CRITICAL)

`python/ferrum/relations.py:187-203` (`install_relation_descriptors`):
- Now installs `_ForwardRelationDescriptor` for **all** relation kinds
  (line 200, was FK/OTO only). Per §5a, forward M2M raises when not loaded.
- Docstring (lines 190-194) documents the §5a contract.

`python/ferrum/relations.py:246-282` (`_ReverseRelationDescriptor`):
- `kind == "m2m"` → raises `FerrumRelationNotLoadedError` (line 273-277). ✓
- Otherwise → returns `QuerySet(related).filter(**{fk_column: pk_val})`
  with no I/O (line 282). ✓
- Docstring (lines 247-262) correctly documents the §5a contract and the
  single-`id` limitation (composite PK not yet supported).

Verified behavior matches §5a:
- Forward FK/OTO/M2M: raise when unloaded ✓
- Reverse M2M: raises ✓
- Reverse FK/OTO: returns unbound QuerySet, no I/O ✓

No identity map (§5a rejection): every terminal returns fresh model
instances via `model_construct` (lines 580, 674) — no session-level cache.

No implicit lazy I/O (§5a rejection): forward relations and reverse M2M
raise; reverse FK/OTO return an unbound QuerySet that requires an explicit
`ConnectionLike` at the terminal.

No unrestricted SQL (§5a rejection): all identifiers from metadata
allowlists; all values are bound parameters (see SecurityEngineer gate).

### Bulk hardening architecture

`python/ferrum/relations.py:119-141` (`safe_batch_size`):
- Clamps `requested` to `max_params // num_fields_per_row` (line 140).
- Default `max_params=65535` matches PostgreSQL's limit.
- `safe_batch_size(3, requested=30000) → 21845` (3 × 21845 = 65535) —
  verified by `test_clamps_to_limit`.

Composite-key bulk operations are tested via the existing
`QuerySet._build_bulk_insert_ir` / `_build_bulk_update_ir` /
`_build_bulk_delete_ir` IR builders in `queryset.py` (import-only —
W1-A/W2-B own). `tests/python/unit/test_bulk_operations.py` covers:
- `test_composite_pk_rows` (bulk insert, 3-field composite rows).
- `test_composite_pk` (bulk update, 2 PK fields + 1 update field).
- `test_composite_pk_rejects_wrong_arity` (validation).
- `test_composite_pk_rejects_scalar` (bulk delete validation).

The architecture correctly delegates IR building to `queryset.py` and adds
only the `safe_batch_size` helper in owned `relations.py` — no shared-path
modifications.

### Cascade documentation

`python/ferrum/relations.py:8-28` (module docstring "Cascade behavior"):
- Documents that Ferrum does NOT implement a SQLAlchemy-style unit-of-work
  cascade.
- ON DELETE actions declared on FK/OTO fields, emitted as `FOREIGN KEY … ON
  DELETE` in DDL, enforced by the database.
- Explicitly states: "no Python-side cascade traversal, no per-instance
  cascade dispatch, and no identity-map ordering of cascade operations."
- Applications needing application-level cascade logic must implement it
  explicitly.

This matches the task acceptance criterion "Document cascade behavior as
database-driven; no unit-of-work cascades" and is consistent with §5a
(Identity map rejection).

### Corrective fix verification (run 20260829T110000Z)

**Defect 1 (false composite-PK docstring):**
```
$ grep -n "Composite primary keys are honored" python/ferrum/relations.py
(no output — false claim removed)
```
`python/ferrum/relations.py:259-261` now reads:
> "The unbound QuerySet is filtered by the FK column using the parent's
> ``id`` field (``getattr(obj, "id")``). Composite primary keys are not yet
> supported — the accessor reads a single ``id`` attribute and filters by
> one FK column."

Accurately describes the code at line 281 (`getattr(obj, "id", None)`).

**Defect 2 (misleading M2M ordering docstring):**
`python/ferrum/relations.py:451-455` (`prefetch_related_objects` docstring)
now scopes the ordering claim to reverse-FK and documents M2M per-parent
list order as through-table link order.

`python/ferrum/relations.py:613-615` (`_prefetch_m2m` docstring) now reads:
> "Target rows are fetched with ``ORDER BY PK`` (Phase 2), but per-parent
> list order follows the through-table link order from Phase 1 (which has
> no ``ORDER BY``), not the target PK order."

Both accurately describe the implementation.

**Defect 3 (dead code):**
```
$ grep -rn "_model_pk_fields\|_instance_pk_values" python/ferrum/ tests/
(no output — dead code removed)
```
`register_reverse` (line 167) now directly follows
`_resolve_through_columns` (line 144) with the standard two-blank-line
separator. No behavior change.

### Tests

```
$ uv run pytest tests/python/unit/test_relations.py tests/python/unit/test_bulk_operations.py -x -q
77 passed in 0.23s
```

Integration (from executor log + verification record, live PostgreSQL):
```
15 passed in 0.48s  (tests/python/integration/test_relations_bulk.py)
```

## Findings

No blocking findings. All acceptance criteria satisfied with architecture
consistent with §2, §5a, and §8.

### Observations (non-blocking, follow-up)

1. **Reverse descriptor composite-PK limitation**: `_ReverseRelationDescriptor.__get__`
   (line 281) hardcodes `getattr(obj, "id", None)`. The corrected docstring
   now accurately documents this. A future enhancement could use
   `ModelMetadata.pk_fields` for composite-PK parent models, but no
   consumer currently needs this — YAGNI applies (§7).

2. **M2M Phase 1 ordering**: through-table link order is non-deterministic
   (no `ORDER BY` in Phase 1 SQL, line 642-645). The corrected docstring
   accurately documents this. If deterministic M2M per-parent ordering
   becomes a consumer requirement, add `ORDER BY {owner_ident},
   {target_ident}` to Phase 1. No current consumer requirement — YAGNI.

3. **`safe_batch_size` placement**: the helper lives in `relations.py`
   but is general-purpose. Keeping it here avoids touching shared paths
   (per task contract). A future refactor could move it to a shared utils
   module, but that is out of W2-C scope.

## Decision

**approved**

The W2-C relationship and bulk behavior architecture is sound and consistent
with the ratified W0-A contracts (§5a). Reverse FK/OTO/M2M loading, nested
prefetch with through models, bounded batching, explicit relation access,
bulk composite-key hardening, and database-driven cascade documentation all
satisfy the task acceptance criteria. The corrective fix run resolved all
three verification defects (false composite-PK docstring, misleading M2M
ordering docstring, dead code) with no behavior change. No new IR nodes
were introduced; `queryset.py` / `models.py` were not modified.

This record grants only the ChiefArchitect gate. It does not substitute
for the SecurityEngineer, CodeReviewer, or independent verification gates.
