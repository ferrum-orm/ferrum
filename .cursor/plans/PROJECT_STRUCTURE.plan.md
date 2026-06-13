# Project Structure Template

> Template for documenting Ferrum's repository layout as production source lands. Owner:
> ChiefArchitect. Keep aligned with `AGENTS.md` §6. Production source is not implemented yet — this
> documents the intended/landed structure, not new code.

## 1. Current layout

```
docs/foundation/   # PRD, architecture feasibility review, security review (authoritative)
docs/design/       # product design review
.cursor/           # Cursor agent config: rules/, skills/, commands/, plans/ (*.plan.md)
.claude/           # Claude Code agent config: rules/, skills/, commands/, plans/ (*.md)
AGENTS.md          # single source of truth for agents
CLAUDE.md          # Claude-tailored, defers to AGENTS.md
README.md          # external pitch + committed API shape
```

## 2. Planned source layout (when it lands)

```
python/ferrum/     # public Python package (or src/ferrum/)
rust/              # maturin-managed Rust compiler/codec crate
tests/             # Python tests (Rust unit tests live with the crate)
pyproject.toml     # Python build manifest
Cargo.toml         # Rust build manifest
```

## 3. Module responsibilities

- For each top-level package/crate module: its single responsibility and boundary placement
  (Python ergonomics/async/I/O vs Rust pure compile/hydrate).

## 4. Build & packaging

- maturin + PyO3; abi3 wheels and CI matrix per ADR-005 (do not pre-empt the matrix breadth).

## 5. Conventions

- Naming, layout, and where new code goes. Tests co-located per language convention.

## 6. Open questions / ADR links

- ADR-005 packaging/CI; any decision recorded in `DECISIONS.md`.
