# Production Readiness Protocol

This is the single source of truth for Cursor and Claude production-readiness
orchestration. Platform-specific skills, rules, agents, commands, goals, and loop
recipes point here and may add only platform invocation details.

## Task lifecycle

Every assigned task records `Specify → Plan → Tasks → Implement` in its contract.
All four stages are mandatory; their depth is proportional to risk. A trivial
task may use one concise paragraph per stage, but no stage is implicit.

Execution begins only when dependencies are complete and the coordinator grants
exclusive ownership. Evidence gathering and draft proposals may precede named
review verdicts; gated source/contract mutations and completion may not.

## Executor lifecycle

Every run follows:

1. **Load** — contract, state, rules, prior evidence, base revision, working tree,
   dependencies, and owned paths.
2. **Execute** — only approved tasks and paths.
3. **Validate executor output** — focused checks plus full `mise run ci-local`,
   with exact commands and meaningful output.
4. **Verify independently** — a different run inspects the diff and executes fresh,
   deterministic checks. Executor summaries are claims, not proof.
5. **Update state** — executor updates workstream state; coordinator reconciles
   independent and named-gate verdicts into aggregate state.

## Authority and hard stops

Generic coordination and verification never replace named authorities:

- ChiefArchitect ratifies ADRs, root architecture contracts, new components, and
  new failure modes.
- SecurityEngineer clears SQL compilation, migration apply, errors/redaction,
  auth/secrets, RLS/admin GUCs, and schema selection.
- ProductManager resolves scope and compatibility-policy decisions.
- CodeReviewer provides the final general code-quality gate.

Loops and executors stop at these surfaces until the required verdict artifact
exists at `reviews/<task-id>/<run-id>-<authority>.md` using `reviews/TEMPLATE.md`
and records `decision: approved`. They may gather evidence and draft proposals;
they may not ratify, merge, commit, push, or self-clear a gate.

The named authority owns verdict content. If its agent is read-only, the
coordinator may persist the returned verdict verbatim with the authority agent id;
the coordinator may not alter the decision or findings.

## Ownership and concurrency

- The coordinator is the sole aggregate-state writer and grants leases in
  `state/index.yaml`.
- One executor owns one workstream state and task log directory at a time.
- Shared paths (`AGENTS.md`, `CLAUDE.md`, `README.md`, exports, manifests,
  `mise.toml`, CI, migration ledger/schema) are serialized through coordinator
  path-scoped leases. Multiple leases may coexist only when their normalized path
  sets do not overlap. Parallel tasks must exclude shared paths unless explicitly leased.
- Unexpected edits, stale leases, or overlapping ownership stop execution.
- Integration order for shared contracts is errors → pool/runtime → sessions →
  query compiler → migrations.

`shared_path_leases` is a map keyed by `lease_id`; each value follows
`state/LEASE_TEMPLATE.yaml`. A lease identifies one workstream, run, holder,
bounded path list, acquisition time, and expiry. The coordinator removes expired
leases before assignment. Any active workstream touching `shared_paths` must
reference its live lease id in `shared_path_lease`; validation fails otherwise.

## Evidence and history

Run and verification records are append-only by protocol. Git becomes the durable
history after commit; records include the commit SHA when one exists. Before
commit, record base revision, exact changed paths, diff/evidence references, and
the smallest safe inverse patch. Never rewrite an earlier record to hide failure.

Verification requires deterministic evidence appropriate to the claim: full local
CI, live PostgreSQL contracts, concurrency/cancellation tests, wheel smoke, schema
drift, or benchmarks. Exit status is recorded, but meaningful output and observed
behavior establish the claim.

## State transitions

Allowed progression:

`specified → planned → tasked → ready → in_progress → validating → awaiting_verification → verified → complete`

`blocked` and `reverted` are explicit states. `complete` requires independent
verification and every named authority gate required by the task.

Plan frontmatter changes only when an entire top-level todo changes state. Detailed
progress belongs to per-workstream state and append-only records.

## Loop bounds

One loop tick advances at most one coordination boundary. Prefer event-driven wakes.
Stop when work is complete, a hard-stop surface is reached, ownership overlaps,
deterministic verification fails, or user/environment input is required. Never run
two implementation ticks for the same workstream concurrently.
