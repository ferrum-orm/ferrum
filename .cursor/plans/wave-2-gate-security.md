# Wave 2 Security Gate Verdict

**Verdict: Pass with Follow-ups** (Wave 3 unblocked)

No Critical findings. All core gates pass:
- ERR-2: catch_unwind on every PyO3 function ✅
- CRED-1: password/DSN redacted in connection errors ✅  
- panic = "unwind" in workspace Cargo.toml ✅
- GIL: no release in compile/hydrate ✅
- Tier A hook allowlist (_TIER_A_KEYS) enforced ✅

Medium follow-ups for Wave 3:
- M-1: acquire()/release()/close() don't remap asyncpg exceptions to Ferrum taxonomy (ERR-1 partial gap)
- M-2: UnsupportedOperator/InvalidSortDirection echo user control strings in error messages
- H-1: No SQLSTATE→Ferrum mapping yet (ADR-006 deferred, ERR-1 stub gap — Wave 3)
