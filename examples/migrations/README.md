# Ferrum Migrations

End-to-end guide for schema changes with Ferrum v0.1.

---

## Overview

Ferrum migrations are Python classes that live in a `migrations/` directory next to your
models. Each file describes one forward schema change. The table `ferrum_migrations` (created
automatically on first run) records which migrations have been applied so the CLI can determine
what's pending.

---

## Prerequisites

```bash
# From the repo root
uv sync --extra dev
mise run dev

cd examples/migrations
cp .env.example .env
docker compose up -d
export FERRUM_DATABASE_URL=postgresql://ferrum:changeme@127.0.0.1:5432/ferrum_dev
```

---

## Quick start

```bash
# 1. Define your models in models.py
# 2. Generate first migration
ferrum makemigrations --name create_note

# 3. Review the generated file
cat migrations/0001_create_note.py

# 4. Apply pending migrations
ferrum migrate

# 5. Check status
ferrum showmigrations
```

---

## Migration file format

`ferrum makemigrations` writes files like this:

```python
from ferrum.migrations import Migration
from ferrum.migrations import operations as ops


class Migration(Migration):
    dependencies = []
    operations = [
        ops.CreateTable("note", [
            ops.Column("id", "INTEGER", primary_key=True, not_null=True),
            ops.Column("body", "TEXT", not_null=True),
        ]),
    ]
```

- `dependencies` — list of migration names that must be applied before this one.
- `operations` — ordered list of schema operations to execute.

Files are numbered sequentially (`0001_`, `0002_`, …). The number is the stable key; the
human-readable name after the underscore is informational.

---

## Adding a column (additive change)

1. Edit `models.py` to add the field:

```python
class Note(Model):
    id: int = 0
    body: str = ""
    title: str = ""        # new
```

2. Generate a new migration:

```bash
ferrum makemigrations --name add_title
```

This writes `migrations/0002_add_title.py` with an `AddColumn` operation. Review it, then
apply:

```bash
ferrum migrate
```

Additive changes (`CreateTable`, `AddColumn`) do not require `--confirm` in a development
environment.

---

## Destructive changes

Operations that destroy data — `DropTable`, `DropColumn`, type narrowing — require explicit
confirmation. Without `--confirm` the CLI exits with an error:

```
FerrumMigrationError: Migration requires explicit confirmation.
  Destructive operations: drop_column(note.title)
  Re-run with --confirm to proceed.
```

Pass `--confirm` to proceed:

```bash
ferrum migrate --confirm
```

This applies to individual operations within a migration, not the whole file. If a migration
contains both additive and destructive ops, the confirmation gate fires for the destructive
ones regardless.

---

## Non-dev environments

Any apply against a non-development environment requires both `--env` and `--confirm`:

```bash
ferrum migrate --env production --confirm
```

Omitting either flag blocks the apply. This is a hard gate — there is no environment variable
override.

---

## Viewing migration status

```bash
ferrum showmigrations
# [X] 0001_create_note
# [ ] 0002_add_title
```

`[X]` = applied and recorded in `ferrum_migrations`. `[ ]` = pending.

---

## CLI reference

| Command                                   | Description                                              |
| ----------------------------------------- | -------------------------------------------------------- |
| `ferrum makemigrations --name NAME`        | Generate the next migration file from model changes      |
| `ferrum migrate`                           | Apply all pending migrations                             |
| `ferrum migrate --confirm`                 | Apply including destructive operations                   |
| `ferrum migrate --env ENV --confirm`       | Apply in a non-development environment                   |
| `ferrum showmigrations`                    | List applied and pending migrations                      |
| `ferrum init [--name DIR]`                 | Scaffold docker-compose, `.env.example`, `.gitignore`    |

---

## Ledger table

Ferrum creates `ferrum_migrations` automatically on the first `ferrum migrate` run. It stores:

- `id` — auto-incrementing primary key.
- `name` — migration filename stem (e.g. `0001_create_note`).
- `applied_at` — UTC timestamp of the apply.

Do not edit this table manually. If you need to mark a migration as applied without executing
it (e.g. baseline an existing schema), that workflow is not yet supported in v0.1.

---

## What's not supported (v0.1)

- **Down-migrations / revert** — there is no `migrate --revert` or rollback command. To undo
  a committed change, write a new forward migration that reverses the effect.
- **Squash / merge** — multiple migration files cannot be collapsed into one automatically.
- **Data migrations (`RunPython`)** — operations that execute arbitrary Python logic are not
  supported. Use a one-off script outside the migration system.
- **Type narrowing** — changing a column to a narrower type is not auto-generated; it requires
  a destructive `DropColumn` + `AddColumn` pair with `--confirm`.

---

## Legacy: JSON plan workflow

The original `ferrum migrations apply PLAN.json` command is still supported as an escape hatch.

```bash
# Dry-run
ferrum migrations apply sample_plans/001_create_note.json --dry-run

# Apply (non-destructive)
ferrum migrations apply sample_plans/001_create_note.json --confirm

# Apply destructive plan
ferrum migrations apply sample_plans/003_drop_note_title.json --confirm
```

The three sample plans in `sample_plans/` demonstrate `create_table`, `add_column`, and
`drop_column`. This interface is kept for compatibility and for cases where a JSON plan is
generated programmatically (see `generate_plan.py`); prefer the Django-style `makemigrations`
/ `migrate` workflow for day-to-day development.

After migrations, run the CRUD demo: [../simple/README.md](../simple/README.md).
