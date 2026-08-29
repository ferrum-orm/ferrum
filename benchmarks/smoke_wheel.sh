#!/usr/bin/env bash
# Build a wheel and prove it installs and imports without a Rust toolchain
# (ADR-005: abi3 wheels, sdist-only compile fallback). The install step below
# must never see `uv`/`maturin`/`cargo` on PATH inside the fresh venv — that
# is the property this script exists to check.
#
# Usage: bash benchmarks/smoke_wheel.sh
#
# Writes:
#   benchmarks/latest/smoke-wheel.xml           (pytest JUnit correctness)
#   benchmarks/latest/smoke-wheel-timing.json   (build/install/test seconds)
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
out_dir="$repo_root/benchmarks/latest"
mkdir -p "$out_dir"

work_dir="$(mktemp -d)"
trap 'rm -rf "$work_dir"' EXIT

# date(1) %N is GNU-only (no-op on macOS BSD date); python3 gives a portable
# sub-second clock on every platform this script runs on (local + CI).
now() { python3 -c 'import time; print(time.time())'; }

# Prefer `uv run maturin` for local dev (mise/uv provide it without any
# system PATH assumptions); fall back to a plain `maturin` on PATH for CI
# runners that already activated a venv with `pip install maturin`.
if command -v uv >/dev/null 2>&1; then
  maturin_cmd=(uv run --project "$repo_root" maturin)
elif command -v maturin >/dev/null 2>&1; then
  maturin_cmd=(maturin)
else
  echo "error: maturin not found (need 'uv' locally, or an activated venv with maturin in CI)" >&2
  exit 1
fi

echo "== Building wheel =="
t0=$(now)
"${maturin_cmd[@]}" build --quiet -o "$work_dir/dist"
t1=$(now)

wheel_path="$(ls "$work_dir"/dist/*.whl | head -n1)"
echo "Built: $wheel_path"

echo "== Installing into a fresh, isolated venv (no maturin/rustc) =="
python3 -m venv "$work_dir/venv"
# shellcheck disable=SC1091
source "$work_dir/venv/bin/activate"
pip install --quiet --disable-pip-version-check "$wheel_path" pytest
t2=$(now)

echo "== Running post-install smoke tests =="
python -m pytest "$repo_root/tests/python/smoke" -m smoke -v \
  --junitxml="$out_dir/smoke-wheel.xml"
t3=$(now)
deactivate

python3 - "$t0" "$t1" "$t2" "$t3" "$out_dir/smoke-wheel-timing.json" "$wheel_path" <<'PY'
import json
import sys

t0, t1, t2, t3, out_path, wheel_path = sys.argv[1:]
report = {
    "smoke_wheel_build_seconds": float(t1) - float(t0),
    "smoke_wheel_install_seconds": float(t2) - float(t1),
    "smoke_wheel_test_seconds": float(t3) - float(t2),
    "wheel_path": wheel_path,
}
with open(out_path, "w") as f:
    json.dump(report, f, indent=2, sort_keys=True)
    f.write("\n")
PY

echo "Wheel smoke timing: $out_dir/smoke-wheel-timing.json"
