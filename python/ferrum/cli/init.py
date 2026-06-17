"""``ferrum init`` — scaffold a new Ferrum project.

Security invariants (INIT-1 / INIT-2):
- Generated docker-compose pins ``127.0.0.1`` as the PostgreSQL bind address.
- Generated ``.gitignore`` excludes ``.env`` files.
- Generated ``.env.example`` contains only placeholder credentials.
- ``init`` refuses writes outside the current working directory allowlist
  (no symlink traversal, no absolute path injection).
"""

from __future__ import annotations

from pathlib import Path

from rich.console import Console
from rich.panel import Panel

_FERRUM_TOML_TEMPLATE = """\
# Ferrum project configuration.
# Secrets (database URL etc.) go in .env, not here.

[ferrum]
# Python module that imports your app's models (enables makemigrations auto-discovery)
# settings = "ferrum_conf"

# Migrations directory (default: ./migrations)
# migrations_dir = "migrations"

# Default environment name used by ferrum migrate
# default_env = "development"

# Path to dotenv file loaded by the CLI (default: .env)
# env_file = ".env"
"""

_GITIGNORE_TEMPLATE = """\
# Ferrum / environment secrets — never commit real .env files
.env
.env.*
!.env.example
"""

_ENV_EXAMPLE_TEMPLATE = """\
# Copy this file to .env and fill in your values.
# Never commit .env to version control.
FERRUM_DATABASE_URL=postgresql://ferrum:changeme@127.0.0.1:5432/ferrum_dev
"""

_DOCKER_COMPOSE_TEMPLATE = """\
version: "3.9"
services:
  db:
    image: postgres:16
    # Bind to 127.0.0.1 only — never expose to the network (INIT-1)
    ports:
      - "127.0.0.1:5432:5432"
    environment:
      POSTGRES_USER: ferrum
      POSTGRES_PASSWORD: changeme
      POSTGRES_DB: ferrum_dev
    volumes:
      - pgdata:/var/lib/postgresql/data
volumes:
  pgdata:
"""

_NEXT_STEPS = """\
1. cp .env.example .env
2. Edit .env with your credentials
3. docker compose up -d
4. maturin develop  # build the Ferrum extension
5. Create ferrum_conf.py to import your models
   (enables makemigrations auto-discovery)"""


def run_init(*, name: str = ".", force: bool = False) -> None:
    """Scaffold a minimal Ferrum project in ``name`` directory.

    Args:
        name: Target directory. Must be relative to cwd (INIT-2).
        force: When ``True``, overwrite existing scaffold files (INIT-2).

    Raises:
        SystemExit: If the path is outside cwd or already contains conflicting files.
    """
    cwd = Path.cwd().resolve()

    if name == ".":
        target = cwd
    else:
        target = (cwd / name).resolve()
        # Refuse any path outside cwd (INIT-2 — no symlink traversal, no absolute injection)
        try:
            target.relative_to(cwd)
        except ValueError:
            print(f"Error: '{name}' resolves outside the current working directory.")
            raise SystemExit(1) from None

    target.mkdir(parents=True, exist_ok=True)

    write = _write_force if force else _write_if_absent
    write(target / "ferrum.toml", _FERRUM_TOML_TEMPLATE)
    write(target / ".gitignore", _GITIGNORE_TEMPLATE)
    write(target / ".env.example", _ENV_EXAMPLE_TEMPLATE)
    write(target / "docker-compose.yml", _DOCKER_COMPOSE_TEMPLATE)

    console = Console()
    console.print(f"Ferrum project scaffolded in [bold]{target}[/bold]")
    console.print(Panel(_NEXT_STEPS, title="Next steps", border_style="blue"))


def _write_if_absent(path: Path, content: str) -> None:
    if path.exists():
        print(f"  skip  {path.name} (already exists)")
    else:
        path.write_text(content, encoding="utf-8")
        print(f"  write {path.name}")


def _write_force(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    action = "overwrite" if path.exists() else "write"
    print(f"  {action} {path.name}")
