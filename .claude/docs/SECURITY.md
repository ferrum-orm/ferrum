# Ferrum Security Requirements & Threat Model

Security requirements and threat model for Ferrum's v0.1 PostgreSQL MVP. Each requirement below
carries an ID and a testable acceptance criterion; the release-qualification gates table near the
end enumerates the must-pass subset. The product contract lives in `./PRODUCT_REQUIREMENTS.md`.

## Threat-model lenses

- **STRIDE:** Tampering (SQL injection), Information Disclosure (logging/errors), Elevation of
  Privilege (destructive migrations).
- **OWASP:** Injection, Security Misconfiguration, Sensitive Data Exposure.
- **Principles:** Secure Defaults, Fail Securely, Complete Mediation, Least Privilege, Input
  Handling (allowlist > denylist).

---

## 1. Parameterized SQL & SQL Generation

Ferrum's Rust engine emits parameterized queries; no user input is interpolated into SQL strings,
and invalid filters fail before SQL execution. The requirements below make those guarantees
explicit and testable.

| ID | Vulnerability class | Attack path | Blast radius | Requirement |
|----|---------------------|-------------|--------------|-------------|
| SQL-1 | **SQL injection via identifier concatenation** | If `filter()`, `order_by()`, or `exclude()` accept runtime field names (e.g., from HTTP query params) and those names are concatenated into SQL without allowlist validation, an attacker supplies `id); DROP TABLE users;--` or subquery injection via the identifier position. Value parameterization does not protect identifier slots. | Full database read/write within connection privileges | All SQL identifiers (tables, columns, operators, sort directions) MUST be resolved from model-metadata allowlists at compile time; runtime strings MUST NOT be concatenated into SQL as identifiers. |
| SQL-2 | **Operator injection** | If filter operator names are user-supplied strings passed to the SQL builder, an attacker may invoke unexpected operators or raw fragments. | Query manipulation, potential data exfiltration | Filter operators are enum-validated; unsupported operators fail at compile time with no SQL emitted. |
| SQL-3 | **Undefined raw-SQL surface** | A silently added `raw()`, `extra()`, or string SQL fragment creates an injection escape hatch that bypasses all parameterization guarantees. | Bypass of all parameterization guarantees | v0.1 Won't-have: no raw SQL passthrough, string fragments, or user-supplied SQL templates. Any future addition requires a separate security review gate. |
| SQL-4 | **No verification requirement** | Parameterization stated but not tested at the product level; a regression reintroduces injection in a future release. | Regression introduces injection | Release qualification includes automated tests asserting no string interpolation of user input across all supported query-compilation paths. |

---

## 2. Credential Handling

Connection failures must not expose credentials. The intent is correct but must be testable — an
implementer can satisfy a generic "no credential exposure" rule while still logging a full
`postgresql://user:pass@host/db` in hook payloads or exception messages.

| ID | Requirement | Test |
|----|-------------|------|
| CRED-1 | Connection strings and passwords MUST NOT appear in default hook payloads, exceptions, or migration output | Fixture DSN with known password; assert no substring match in default error/hook serialization |
| CRED-2 | Connection diagnostics MAY include host, port, database name, username — never password or full DSN | Assert redacted diagnostic shape matches the allowlist |
| CRED-3 | Migration dry-run SQL MUST NOT embed connection credentials | Scan dry-run output for DSN patterns |

---

## 3. Observability, Logging & PII

Default logging avoids raw bound-parameter values, hook payloads exclude raw parameter values by
default, and execution failures must not leak raw row data. The requirements below tighten the
defaults and the contract for third-party consumers.

| ID | Vulnerability class | Attack path | Blast radius | Requirement |
|----|---------------------|-------------|--------------|-------------|
| LOG-1 | **PII/credential disclosure via ambiguous "SQL context"** | Hooks capture "SQL context" without defining its contents; an implementer may log interpolated SQL (values substituted) or full connection strings in hook payloads. | PII, credentials, regulated data in centralized logs | Define **SQL context** explicitly: parameterized query text, parameter count, and parameter type summary only. Bound values MUST NOT appear in default hook payloads under any key. |
| LOG-2 | **Validation error value echo** | Field-specific validation errors typically echo submitted values. For password, token, SSN, or email fields, errors become log/exception PII sinks. | PII in logs, APM traces, error-reporting services | Validation errors identify field names and constraint violations without echoing submitted values by default; verbose value echo requires explicit debug opt-in. |
| LOG-3 | **No environment-aware secure defaults** | A single default for all environments; teams enable verbose logging in prod to debug incidents. | Widespread PII exposure during incidents | Production-safe defaults are minimal; verbose SQL/parameter logging requires an explicit configuration flag documented as unsafe for production. |
| LOG-4 | **Third-party hook contract missing** | Custom observability integrations receive hook payloads without documented security obligations. | Downstream log pipelines store secrets | The hook integration guide MUST warn against logging bound parameters, DSNs, or hydrated model bodies. |

### Observability tiers

"SQL context" is tiered so operators can choose safe production defaults vs. verbose dev. Tier B/C
require an explicit Ferrum-specific opt-in and MUST NOT activate from a generic `DEBUG=1`.

| ID | Requirement |
|----|-------------|
| DBG-2 | Define tiers — **Tier A (default/prod):** query fingerprint, operation/model metadata, duration, status, failure category, param count/types. **Tier B (opt-in dev):** full parameterized SQL text. **Tier C (explicit, local-dev only):** bound values — never a default, never safe for APM/centralized logs/production. Redacted values use `[REDACTED]` placeholders without length or prefix hints; collection/JSON payloads in hooks and errors are size-capped to mitigate side channels. |

---

## 4. Error & Debug Surfaces

Errors should be actionable without leaking secrets or Ferrum source references, with structured
compilation errors and failure classification. The requirements below define the sanitization and
the debug/production boundary.

| ID | Vulnerability class | Attack path | Blast radius | Requirement |
|----|---------------------|-------------|--------------|-------------|
| ERR-1 | **PostgreSQL native error passthrough** | PostgreSQL `DETAIL` clauses include duplicate-key values, FK violations with referenced values, and constraint names revealing schema. Unsanitized passthrough to Python exceptions propagates to HTTP 500 responses and APM. | Row-level data disclosure, schema reconnaissance | Database execution errors are mapped to stable, sanitized Ferrum error types before surfacing to callers; raw PostgreSQL `DETAIL`/`HINT` containing data values MUST NOT be exposed by default. |
| ERR-2 | **No debug/production error boundary** | No distinction in operational error verbosity. | Internal schema/SQL details exposed to API consumers in production | Verbose diagnostic fields (compiled SQL, internal operation names) are available only under explicit debug configuration; default error payloads are production-safe. |
| ERR-3 | **PyO3/Rust panic mapping unspecified** | Rust panics through PyO3 can expose internal paths or abort the process. | Process crash, information leak in stderr | Rust panics and internal errors map to catchable Python exceptions without memory addresses, file paths, or process abort in normal operation. |
| ERR-4 | **Existence oracle via `get()` errors** | `get()` not-found vs. multiple-found errors can reveal record existence/count to unauthenticated callers when used in the API layer. | Low-grade information disclosure | Document that `get()` error semantics are existence oracles; authorization must be enforced before query execution at the application layer (the ORM cannot fix this alone). |

---

## 5. Migration Safety

Migrations support dry-run output before apply, fail unsafe/unsupported migrations with guidance,
require explicit safety calls for unscoped destructive operations, and report whether the DB
changed on failure. The requirements below close the destructive-operation and production-apply
gaps.

| ID | Vulnerability class | Attack path | Blast radius | Requirement |
|----|---------------------|-------------|--------------|-------------|
| MIG-1 | **Destructive operations without mandatory confirmation** | An operator runs `apply` without review → DROP COLUMN/TABLE, data loss. | Irreversible data loss, production outage | Dry-run is must-have. Destructive operations (column drop, table drop, type narrowing, `NOT NULL` on a populated column) require an explicit confirmation flag; dry-run output MUST label destructive steps prominently. |
| MIG-2 | **Data-destructive type changes not classified** | Field type changes can truncate or nullify data without the same gates as removals. | Silent data corruption | Classify narrowing type changes as destructive; apply the same confirmation gates as removals. |
| MIG-3 | **No production apply guard** | Apply API/commands lack an environment safeguard; CI/CD misconfiguration applies migrations against prod unintentionally. | Full schema/data loss | Migration apply against non-development targets requires explicit environment confirmation (flag, env var, or interactive confirm) documented in the migration workflow. |
| MIG-4 | **Partial migration state underspecified** | Failure messages state whether the DB changed, but there is no requirement for transactional step boundaries or a recovery playbook. | Inconsistent schema, application crash loop | Each migration step documents its atomicity expectations; partial-failure states include documented recovery actions. |
| MIG-5 | **Bulk delete/update safety underspecified** | A bulk `delete()` or `update()` without filters deletes or mutates en masse. | Mass data deletion/mutation | Any `delete()` or `update()` without filters requires an explicit named danger API (`danger_delete_all()` / `danger_update_all()` or similar) and fails by default. |

### CI/CD confirmation token

The non-development apply gate (MIG-3) is satisfied by a dry-run-scoped confirmation token rather
than a generic destructive bypass. `--force` / `--yes` / env-var-only bypass are banned. These are
threat-model requirements the architecture must encode.

| ID | Vulnerability class | Requirement |
|----|---------------------|-------------|
| MIG-6 | Token forgery / replay | The confirmation token cannot be derived from migration files alone — it is bound (HMAC or equivalent keyed over plan hash + environment + sanitized DB identity + migration ledger fingerprint) to the exact plan, target, and DB identity. Re-applying with the same token after a successful apply fails before mutation (single-use or ledger nonce). `--confirm-environment` must match the environment embedded in the token, not merely be a separate operator-typed string. |
| MIG-7 | Capability disclosure | Dry-run JSON documents that the confirmation token grants apply authorization; the token is an authorization capability, not a credential. CI examples store the token as a secret, not as plain log output, and treat dry-run JSON as sensitive in CI logs. |
| MIG-8 | argv leakage | Non-interactive apply accepts the token via a documented secret-injection path (env var / stdin) in addition to a CLI flag; docs warn against shell-history and process-list exposure. |

---

## 6. Project Initialization Scaffold

`ferrum init` generates local-dev scaffolding. It must ship secret-free and minimize attack
surface.

| ID | Vulnerability class | Requirement |
|----|---------------------|-------------|
| INIT-1 | Local exposure | The generated `docker-compose.yml` binds PostgreSQL to `127.0.0.1` (not `0.0.0.0`) unless explicitly documented otherwise. |
| INIT-2 | Secret hygiene | The generated `.gitignore` excludes `.env` and other local secret paths; init write paths are limited to a documented cwd-relative allowlist. Scaffolding uses synthetic placeholders only — secret-free `.env.example`, no real DSNs/passwords in tracked files. Generation is idempotent with no silent overwrite (overwrite limited to an explicit scaffolding-specific flag). |

---

## 7. SQL Inspection & Query Debugging

Query inspection UX must not become a PII/secrets sink.

| ID | Vulnerability class | Attack path | Blast radius | Requirement |
|----|---------------------|-------------|--------------|-------------|
| DBG-1 | **Sensitive data exposure via query inspection UX** | Full SQL + bound values in browser-visible debug panels leak PII to anyone with dev-tool access; in shared staging, to all developers. | PII/secrets in browser, screenshots, support tickets | v0.1 query inspection MUST be local-dev only (not exposed via HTTP routes); the default view shows the parameterized template + parameter metadata, not bound values. |

---

## Release-qualification security gates

These MUST be testable in CI / release qualification before release.

| ID | Class | Severity | Testable acceptance criterion |
|----|-------|----------|--------------------------------|
| SQL-1 | Identifier injection | Critical | Compile-time allowlist rejects unknown field names; no identifier concatenation from runtime strings |
| SQL-3 | Raw SQL escape hatch | High | v0.1 Won't-have explicitly bans raw SQL; CI/doc scan confirms absence |
| CRED-1 | Credential disclosure | High | Known-password DSN absent from default errors/hooks/migration output |
| LOG-1 | PII in observability | High | Default hook payload schema excludes bound values under any key |
| LOG-2 | PII in validation errors | Medium | Field errors contain name + constraint, not the submitted value |
| ERR-1 | DB error data leak | High | PostgreSQL `DETAIL` with row values mapped to generic messages by default |
| MIG-1 | Destructive migration without confirm | Critical | Apply without dry-run review + confirm flag fails for destructive ops |
| MIG-3 | Prod apply misconfig | High | Non-dev apply requires explicit environment confirmation |
| MIG-5 | Unscoped bulk delete | High | `delete()`/`update()` without filters requires a named danger API |
| DBG-1 | Query inspection exposure | Medium | No production HTTP route serves bound parameter values |

---

## Requirements summary

A consolidated view of the security scope, grouped by area:

1. **SQL compilation:** identifiers resolved from model-metadata allowlists; values only via bound
   parameters; no raw SQL in v0.1; operator enum validation; automated non-interpolation tests in
   release qualification. (SQL-1–4)
2. **Credential handling:** DSN/password redaction in defaults; connection-diagnostics allowlist.
   (CRED-1–3)
3. **Observability defaults:** SQL-context tiers (Tier A default; Tier B/C explicit opt-in, never
   from `DEBUG=1`; Tier C local-dev only); no bound values in default hooks; production-safe
   defaults; verbose logging opt-in only; hook integration security guide. (LOG-1–4, DBG-2)
4. **Error handling:** sanitized DB error mapping; debug-only verbose fields; safe PyO3 error
   boundary; validation errors without value echo; documented `get()` existence-oracle semantics.
   (ERR-1–4)
5. **Migrations:** dry-run is must-have; destructive-op confirmation required; type narrowing
   classified destructive; non-dev apply requires explicit confirmation (unforgeable, single-use
   token bound to plan/target/DB identity); per-step atomicity/recovery documented; unscoped bulk
   mutations require an explicit danger API. (MIG-1–8)
6. **Init scaffold:** secret-free output; localhost-bound PostgreSQL; `.env` gitignored;
   idempotent generation. (INIT-1–2)
7. **Query inspection:** local-dev only for v0.1; no HTTP-exposed full-SQL-plus-values view.
   (DBG-1)

### Transport security (architecture phase)

Production connection documentation should require TLS, with `sslmode` guidance for production
deployments. Architecture must also confirm the immutable compiled-state assumption holds under
PyO3 shared state.

---

## Residual risks

- Dynamic raw SQL in v0.2+ remains a future risk; schema-qualified identifier edge cases need
  architecture-level review.
- Operators can opt into verbose logging (Tier B/C) locally; document loudly. Hydrated model
  `repr()` in application code remains out of ORM scope.
- Application code logging its own config remains out of scope; cover in the hook integration
  guide. Hook consumers can still mis-log if the integration guide is weak.
- Stack traces in dev mode and application-layer error handlers re-adding detail remain possible.
- Application-layer authorization before `get()` remains the application's responsibility.
- Operators may bypass migration confirmation flags; backups remain an application/ops
  responsibility. CI operators with log + DB access can replay a confirmation token before apply
  completes (mitigated by single-use/ledger-nonce binding).
- Local docker-compose weak passwords are acceptable for dev-only with documentation; the
  scaffolding overwrite flag is a process risk, mitigated by its scaffolding-only scope.
- Developers can still `print(await queryset.explain())` in application code; document as an
  anti-pattern.
