---
description: Preserve ownership, evidence, and resumability for Ferrum production-readiness work
alwaysApply: true
---

# Production-readiness state

- The coordinator alone edits `.agent-work/production-readiness/state/index.yaml`.
- Shared paths require one active coordinator lease and are serialized.
- An executor edits only its assigned task, workstream state, owned source paths,
  and new immutable run log.
- A verifier writes only a new verification record and source/test fixes explicitly
  reassigned by the coordinator.
- Existing execution and verification records are append-only by protocol.
  Corrections create a new run id; Git is durable history after commit.
- Logs record base revision, pre-existing changes, decisions, files, exact checks,
  meaningful output, failures, residual risks, and a safe inverse change.
- Preserve user and other-agent work. Stop on unexpected edits or path overlap.
- Update plan frontmatter only when an entire plan todo changes status; detailed
  progress belongs in per-workstream state.
- Claims require fresh evidence. Exit status alone is insufficient where output,
  live PostgreSQL behavior, concurrency, or security properties prove the claim.
