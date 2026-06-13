# Data Modeling Template — <Model / Schema>

> Template for Ferrum data-model design. Owner: ChiefArchitect. Models are Pydantic v2 and are the
> single source of truth for validation, serialization, and persistence — no parallel schema.

## 1. Purpose

What this model/schema represents and the queries/use cases it must serve (design for access
patterns up front — Data Gravity).

## 2. Pydantic v2 model (intent)

- Fields, types, and validation derived from the model. Note required vs optional, defaults, and
  constraints. The model is the single source of truth.

## 3. PostgreSQL mapping

- Table name, columns, types, nullability.
- Primary key, unique constraints, foreign keys (PostgreSQL only).
- Indexes justified by the access patterns in §1.

## 4. Metadata derivation

- How model metadata (the read-only allowlist used by the Rust compiler) is built at class
  definition time. Identifiers that become allowlisted; values that stay bound.

## 5. Schema evolution plan

- Additive, backward-compatible by default. How future changes migrate (link to a migration plan).
  Destructive changes (drop/narrow/`NOT NULL`-on-populated) and their confirmation gates.

## 6. Validation & hydration

- Validation on input (Pydantic v2). Hydration on output uses construct-without-revalidate by
  default (ADR-003) — note the trusted-source assumption and any custom-validator caveat.

## 7. Security & privacy

- Sensitive fields; redaction in logs/hooks/errors (no row data by default).

## 8. Open questions / ADR links

- Dependencies on ADR-002 (IR) / ADR-003 (hydration); decisions recorded in `DECISIONS.md`.
