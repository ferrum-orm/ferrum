# Ferrum Agent Team

Specialized subagents for building Ferrum. Governance agents are read-only reviewers;
implementation agents write code; quality agents gate merge.

| Agent                   | Role                                             | When to use                                                   |
| ----------------------- | ------------------------------------------------ | ------------------------------------------------------------- |
| **chief-architect**     | Architecture, ADRs, boundary placement           | New components, IR changes, data models, ADR dependencies     |
| **security-engineer**   | Security gates (SQL, secrets, hooks, migrations) | SQL compilation, auth/secrets, migration apply, observability |
| **product-manager**     | PRD scope and acceptance criteria                | Scope changes, feature prioritization, requirement conflicts  |
| **product-designer**    | Developer experience and onboarding              | Public API shape, error messages, docs/onboarding flow        |
| **python-orm-engineer** | Python package implementation                    | Models, QuerySet, async I/O, hooks, migration orchestration   |
| **rust-core-engineer**  | Rust compiler/codec implementation               | IR validation, SQL compilation, hydration, PyO3 boundary      |
| **code-reviewer**       | Pre-merge contract review                        | After implementation, before merge                            |
| **test-engineer**       | Tests and security test paths                    | New behavior, regression tests, security gate coverage        |

## Typical workflow

1. **Scope** — `product-manager` confirms PRD fit (optional for small tasks).
2. **Design** — `chief-architect` or parent uses `/design-feature` for non-trivial work.
3. **Impact** — `chief-architect` runs architecture-impact before crossing boundaries.
4. **Implement** — `python-orm-engineer` and/or `rust-core-engineer` (respect the boundary).
5. **Security** — `security-engineer` for SQL, secrets, hooks, or migration apply changes.
6. **Test** — `test-engineer` adds coverage; no feature ships without tests.
7. **Review** — `code-reviewer` before merge.

## How to invoke (Cursor)

Ask the parent agent to delegate:

```
Use the chief-architect subagent to assess whether this QuerySet change needs ADR-002 review
```

```
Use the python-orm-engineer subagent to implement the connection pool per ARCHITECTURE.md
```

```
Use the code-reviewer subagent to review my branch changes
```

Or run a governance pass after implementation:

```
Have security-engineer and code-reviewer review the migration apply changes
```

## Contract

All agents defer to `AGENTS.md` at the repo root. Authoritative docs live in `.claude/docs/`.
Cursor-specific rules: `.cursor/rules/`; skills: `.cursor/skills/`; commands: `.cursor/commands/`.
