# Ferrum Agent Team (Claude Code)

Specialized subagents for building Ferrum. Claude Code auto-dispatches from each agent's
`description`; you can also ask explicitly: "Use the chief-architect agent to …"

| Agent                   | Role                                   | When to use                                  |
| ----------------------- | -------------------------------------- | -------------------------------------------- |
| **chief-architect**     | Architecture, ADRs, boundary placement | New components, IR changes, data models      |
| **security-engineer**   | Security gates                         | SQL, secrets, hooks, migration apply         |
| **product-manager**     | PRD scope                              | Scope, prioritization, requirement conflicts |
| **product-designer**    | Developer experience                   | Public API, errors, onboarding, docs         |
| **python-orm-engineer** | Python implementation                  | Models, QuerySet, async I/O, hooks           |
| **rust-core-engineer**  | Rust implementation                    | Compiler, hydration, PyO3 boundary           |
| **code-reviewer**       | Pre-merge review                       | After implementation, before merge           |
| **test-engineer**       | Test coverage                          | Behavior, security gates, regressions        |

## Typical workflow

1. **Scope** — `product-manager` for non-trivial features.
2. **Design** — parent or `chief-architect`; use `.claude/commands/design-feature.md`.
3. **Implement** — `python-orm-engineer` and/or `rust-core-engineer`.
4. **Security** — `security-engineer` for SQL/secrets/migration paths.
5. **Test** — `test-engineer`; no feature without tests.
6. **Review** — `code-reviewer` before merge.

## Contract

All agents defer to `AGENTS.md`. Authoritative docs: `.claude/docs/`. Rules: `.claude/rules/`;
skills: `.claude/skills/`; commands: `.claude/commands/`.

Cursor equivalents live in `.cursor/agents/` with the same names and roles.
