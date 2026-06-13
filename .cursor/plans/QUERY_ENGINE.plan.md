# Query Engine Template — <Capability>

> Template for designing the QuerySet → IR → SQL path. Owner: ChiefArchitect + Backend Engineer.
> QuerySet builds a typed IR; Rust compiles it. No SQL strings in Python; no escape hatches.

## 1. Capability

The query capability being added (operator, lookup, ordering, projection, aggregation, etc.) and
the developer-facing semantics (Django-inspired, async terminal operations).

## 2. Public QuerySet surface

- Method signatures and chaining semantics. Lazy until awaited. Least Astonishment.

## 3. Allowlist & validation

- Fields, operators, and sort directions validated against model metadata. Bad input fails with
  structured errors **before** IR finalization and SQL emission. List the rejection cases.

## 4. IR lowering

- How the QuerySet operation lowers into the typed, versioned IR (ADR-002). Identifiers and values
  kept out-of-band. Show the IR delta.

## 5. Rust compilation

- How the compiler turns this IR into `{ sql_text, bound_params, param_type_summary }`. Pure,
  synchronous, stateless. Parameter binding strategy.

## 6. Execution & hydration

- Async execution at the Python driver await point; cancellation/timeout handling. Hydration via
  construct-without-revalidate (ADR-003).

## 7. Observability

- Tier A query fingerprint/metadata/duration emitted by default. No bound values in default
  payloads.

## 8. Performance

- Expected cost; where it sits on the hot path; benchmark plan (see the benchmark command).

## 9. Tests

- QuerySet → IR shape, allowlist rejection, Rust compile correctness, async terminal behavior,
  integration against PostgreSQL.

## 10. ADR links / open questions

- Dependencies on ADR-002/003; decisions recorded in `DECISIONS.md`.
