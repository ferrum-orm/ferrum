---
name: ferrum-production-readiness
status: active
plan: .cursor/plans/ferrum-production-readiness_6b5f422d.plan.md
state: .agent-work/production-readiness/state/index.yaml
---

# Goal

Make Ferrum a production-grade async-native PostgreSQL ORM that can become the
primary Python ORM in Ticket Analyzer and an async-refactored Org AI Platform.

## Success

Success is exactly the plan's Definition of production-ready. Aggregate progress
and release gates live in the state file; this goal file is a stable pointer and
must not duplicate per-workstream status.

## Execution contract

Tasks use `Specify → Plan → Tasks → Implement`. Runs use
`Load → Execute → Validate executor output → Verify independently → Update state`.
