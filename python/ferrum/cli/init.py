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

_GITIGNORE_TEMPLATE = """\
# Ferrum / environment secrets — never commit real .env files
.env
.env.*
!.env.example
"""

_ENV_EXAMPLE_TEMPLATE = """\
# Copy this file to .env and fill in your values.
# Never commit .env to version control.
DATABASE_URL=postgresql://ferrum:changeme@127.0.0.1:5432/ferrum_dev
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


def run_init(*, name: str = ".") -> None:
    """Scaffold a minimal Ferrum project in ``name`` directory.

    Args:
        name: Target directory. Must be relative to cwd (INIT-2).

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

    _write_if_absent(target / ".gitignore", _GITIGNORE_TEMPLATE)
    _write_if_absent(target / ".env.example", _ENV_EXAMPLE_TEMPLATE)
    _write_if_absent(target / "docker-compose.yml", _DOCKER_COMPOSE_TEMPLATE)

    print(f"Ferrum project scaffolded in {target}")
    print("Next steps:")
    print("  1. cp .env.example .env")
    print("  2. Edit .env with your credentials")
    print("  3. docker compose up -d")
    print("  4. maturin develop  # build the Ferrum extension")


def _write_if_absent(path: Path, content: str) -> None:
    if path.exists():
        print(f"  skip  {path.name} (already exists)")
    else:
        path.write_text(content, encoding="utf-8")
        print(f"  write {path.name}")
