#!/usr/bin/env bash
# bump-version.sh — bump ferrum-orm version across all release metadata files.
#
# Updates:
#   pyproject.toml              (version field)
#   python/ferrum/__init__.py   (__version__)
#   Cargo.toml                  ([workspace.package].version)
#   CHANGELOG.md                (release [Unreleased] under new version)
#   uv.lock                      (via `uv lock`)
#
# Usage:
#   bin/bump-version.sh                # patch bump (default)
#   bin/bump-version.sh --minor
#   bin/bump-version.sh --major
#   bin/bump-version.sh --set 0.2.0
#   bin/bump-version.sh --dry-run
#   bin/bump-version.sh --skip-lock
#   bin/bump-version.sh --date 2026-09-01
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

PYPROJECT="$REPO_ROOT/pyproject.toml"
INIT_PY="$REPO_ROOT/python/ferrum/__init__.py"
CARGO_TOML="$REPO_ROOT/Cargo.toml"
CHANGELOG="$REPO_ROOT/CHANGELOG.md"

DRY_RUN=0
SKIP_LOCK=0
BUMP_PART="patch"
SET_VERSION=""
RELEASE_DATE="$(date +%Y-%m-%d)"

usage() {
  sed -n '3,17p' "$0" | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --patch)   BUMP_PART="patch"; shift ;;
    --minor)   BUMP_PART="minor"; shift ;;
    --major)   BUMP_PART="major"; shift ;;
    --set)     SET_VERSION="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --skip-lock) SKIP_LOCK=1; shift ;;
    --date)    RELEASE_DATE="$2"; shift 2 ;;
    -h|--help) usage 0 ;;
    *) echo "error: unknown argument: $1" >&2; usage 1 ;;
  esac
done

if [[ ! -f "$PYPROJECT" ]]; then
  echo "error: pyproject.toml not found at $PYPROJECT" >&2
  exit 1
fi

CURRENT_VERSION=$(grep -m1 '^version = "' "$PYPROJECT" | sed 's/^version = "\(.*\)"/\1/')

if [[ -z "$CURRENT_VERSION" ]]; then
  echo "error: could not find version in pyproject.toml" >&2
  exit 1
fi

if [[ -n "$SET_VERSION" ]]; then
  NEW_VERSION="$SET_VERSION"
  BUMP_LABEL="set"
else
  IFS='.' read -r MAJOR MINOR PATCH <<< "$CURRENT_VERSION"
  case "$BUMP_PART" in
    patch) NEW_VERSION="$MAJOR.$MINOR.$((PATCH + 1))" ;;
    minor) NEW_VERSION="$MAJOR.$((MINOR + 1)).0" ;;
    major) NEW_VERSION="$((MAJOR + 1)).0.0" ;;
  esac
  BUMP_LABEL="$BUMP_PART"
fi

if [[ "$NEW_VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then :; else
  echo "error: invalid semver: $NEW_VERSION" >&2
  exit 1
fi

if [[ "$NEW_VERSION" == "$CURRENT_VERSION" ]]; then
  echo "version unchanged: $CURRENT_VERSION"
  exit 0
fi

echo "$CURRENT_VERSION -> $NEW_VERSION ($BUMP_LABEL)"
if [[ $DRY_RUN -eq 1 ]]; then
  echo "dry run — no files will be modified"
fi

update_file() {
  local file="$1" content="$2"
  if [[ $DRY_RUN -eq 1 ]]; then
    echo "  would update $(realpath --relative-to="$REPO_ROOT" "$file" 2>/dev/null || echo "$file")"
  else
    printf '%s' "$content" > "$file"
    echo "  updated $(realpath --relative-to="$REPO_ROOT" "$file" 2>/dev/null || echo "$file")"
  fi
}

# --- pyproject.toml ---
pyproject_text=$(cat "$PYPROJECT")
pyproject_updated=$(printf '%s' "$pyproject_text" | sed "s/^version = \".*\"/version = \"$NEW_VERSION\"/")
update_file "$PYPROJECT" "$pyproject_updated"

# --- python/ferrum/__init__.py ---
init_text=$(cat "$INIT_PY")
init_updated=$(printf '%s' "$init_text" | sed "s/^__version__ = \".*\"/__version__ = \"$NEW_VERSION\"/")
update_file "$INIT_PY" "$init_updated"

# --- Cargo.toml ([workspace.package].version) ---
cargo_text=$(cat "$CARGO_TOML")
cargo_updated=$(printf '%s' "$cargo_text" | awk -v new_ver="$NEW_VERSION" '
  /^\[workspace\.package\]$/ { in_wp=1; print; next }
  in_wp && /^\[/ && !/^\[workspace\.package\]/ { in_wp=0 }
  in_wp && /^version = / { sub(/"[0-9]+\.[0-9]+\.[0-9]+"/, "\"" new_ver "\""); print; next }
  { print }
')
update_file "$CARGO_TOML" "$cargo_updated"

# --- CHANGELOG.md ---
if ! grep -q '## \[Unreleased\]' "$CHANGELOG"; then
  echo "error: CHANGELOG.md is missing an [Unreleased] section" >&2
  exit 1
fi

changelog_text=$(cat "$CHANGELOG")

# Split at "## [Unreleased]" and find the next "---\n\n## [" divider after it.
unreleased_marker="## [Unreleased]"
before="${changelog_text%%$unreleased_marker*}"
after="${changelog_text#*$unreleased_marker}"

# Find the divider line (---) that separates Unreleased from prior releases
divider_pos=$(printf '%s' "$after" | grep -n '^---' | head -1 | cut -d: -f1)
if [[ -z "$divider_pos" ]]; then
  echo "error: CHANGELOG [Unreleased] section is not followed by a release divider" >&2
  exit 1
fi

unreleased_body=$(printf '%s' "$after" | head -n "$((divider_pos - 1))" | sed '/^$/d;/^## \[Unreleased\]/d')
rest=$(printf '%s' "$after" | tail -n +$((divider_pos + 1)))

fresh_unreleased="## [Unreleased]

### Added

### Changed

### Fixed

---

"
released_header="## [$NEW_VERSION] - $RELEASE_DATE
"

changelog_updated="${before}${fresh_unreleased}${released_header}${unreleased_body}

${rest}"
update_file "$CHANGELOG" "$changelog_updated"

# --- uv.lock ---
if [[ $SKIP_LOCK -eq 1 ]]; then
  echo "  skipped uv lock (--skip-lock)"
elif [[ $DRY_RUN -eq 1 ]]; then
  echo "  would run: uv lock"
else
  echo "  running: uv lock"
  (cd "$REPO_ROOT" && uv lock)
fi

if [[ $DRY_RUN -eq 0 ]]; then
  echo ""
  echo "Done. Review CHANGELOG, then tag: git tag v$NEW_VERSION"
fi
