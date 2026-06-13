# Migration-gate follow-ups (from docs generation + SecurityEngineer review)

> Source: doc-generation pass over `docs/` + SecurityEngineer review of the
> migration-apply gate path (2026-06-13). Migration apply is a security-sensitive
> area (AGENTS.md §3, `.claude/rules/architecture.md`). Notify SecurityEngineer on
> any change here.

## Resolved in this pass

- **[DONE] Phantom error code `FERR-M002`.** The token-failure message in
  `orchestrator.py` embedded `[FERR-M002]`, but `FerrumMigrationError.code` is
  `FERR-M001` and no `FERR-M002` exists. Reconciled the inline tag to `FERR-M001`
  and updated `tests/python/unit/test_migrations.py:452` to match.
- **[DONE] Forged-flag regression test.** Added
  `test_destructive_op_with_forged_requires_confirmation_false_still_raises`
  (`tests/python/unit/test_migrations.py`): a `drop_table` op with
  `requires_confirmation=False` must still be blocked without `confirm=True`.
  This pins the orchestrator invariant "independently scan op kinds; never trust
  the plan's `requires_confirmation` flag."
- **[DONE] CLI token-implies-confirm coverage.** Added
  `tests/python/unit/test_migrations_cli.py` covering the
  `confirm = args.confirm or (token is not None)` derivation in
  `run_migrations` (`cli/migrations_cmd.py:28`) for: token flag, env-var token,
  no-token, and explicit `--confirm`.
- **[DONE] Docs corrected.** `docs/architecture.md` migration diagram now shows the
  token gate as conditional (not mandatory); `docs/getting-started.md` and
  `docs/api-reference.md` clarify a token is optional and validation also requires
  `confirm=True`; getting-started §8 documents the CLI token-implies-confirm note.

## Open — needs decision (do NOT self-clear)

### 1. Should the CLI require explicit `--confirm` even when a token is present?  (MED)

`cli/migrations_cmd.py:28`:

```python
confirm = getattr(args, "confirm", False) or (token is not None)
```

Today, supplying `--token` / `FERRUM_MIGRATION_TOKEN` implicitly satisfies
`confirm=True`, so a destructive or non-dev apply can proceed without an explicit
`--confirm` on the command line (the token is still validated against the plan
digest, so this is **not** an unauthenticated bypass).

**Decision needed (ChiefArchitect / SecurityEngineer):**

- **Option A — keep current behavior.** Token presence implies intent to apply.
  Now documented and test-pinned. Lowest friction.
- **Option B — defense in depth.** Require `--confirm` explicitly *and* treat the
  token purely as plan-digest verification (token = "this exact plan," confirm =
  "yes, apply now"). One-line change at `cli/migrations_cmd.py:28` plus a test
  flip; docs note in getting-started §8 would be removed.

If Option B is chosen, the docs note added in `docs/getting-started.md` §8 (CLI
note) and the `test_*_implies_confirm` CLI tests must be updated to match.

## Notes

- The Python-layer gate ordering (token → destructive → env → execute) and the
  independent ops scan were confirmed correct by review; no behavior change needed
  there beyond the open decision above.
