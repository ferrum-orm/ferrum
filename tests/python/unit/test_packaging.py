"""Unit tests for packaging metadata and release policy invariants.

These tests validate that ``pyproject.toml`` and the Ferrum package metadata
satisfy the release-readiness contract from W4-D / ADR-005 without requiring
a built wheel or a live database. They are pure-Python, run under the default
unit-test suite (no ``-m integration`` / ``-m security`` markers needed), and
assert:

- ``requires-python`` and the Python classifier list agree and include 3.14.
- The maturin build config uses ``pyo3/abi3-py311`` and ``extension-module``
  so a single abi3 wheel covers CPython 3.11+.
- The ``__version__`` exported by ``ferrum`` matches the version declared in
  ``pyproject.toml`` (single source of truth — no drift).
- The sdist carries ``LICENSE`` (maturin ``include``) so PyPI metadata 2.4
  does not reject the sdist for a missing ``License-File``.
- The license classifier matches the declared license.
"""

from __future__ import annotations

import pathlib
import tomllib

import pytest

import ferrum

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
_PYPROJECT = _REPO_ROOT / "pyproject.toml"


@pytest.fixture(scope="module")
def pyproject() -> dict:
    with _PYPROJECT.open("rb") as f:
        return tomllib.load(f)


class TestPythonVersionSupport:
    """The supported-Python contract advertised by pyproject.toml."""

    def test_requires_python_floor_is_311(self, pyproject: dict) -> None:
        floor = pyproject["project"]["requires-python"]
        assert "3.11" in floor, f"requires-python must target 3.11+, got {floor!r}"

    def test_classifiers_include_all_supported_minors(self, pyproject: dict) -> None:
        classifiers = pyproject["project"]["classifiers"]
        for minor in ("3.11", "3.12", "3.13", "3.14"):
            tag = f"Programming Language :: Python :: {minor}"
            assert tag in classifiers, f"missing classifier {tag!r}"

    def test_alpha_status_classifier(self, pyproject: dict) -> None:
        classifiers = pyproject["project"]["classifiers"]
        assert "Development Status :: 3 - Alpha" in classifiers, (
            "v0.1 must advertise Alpha status (§5a compatibility policy)"
        )


class TestMaturinAbi3Config:
    """The abi3 wheel build contract (ADR-005)."""

    def test_abi3_py311_feature(self, pyproject: dict) -> None:
        features = pyproject["tool"]["maturin"]["features"]
        joined = " ".join(features)
        assert "pyo3/abi3-py311" in joined, (
            "maturin features must include pyo3/abi3-py311 for a single abi3 wheel"
        )

    def test_extension_module_feature(self, pyproject: dict) -> None:
        features = pyproject["tool"]["maturin"]["features"]
        joined = " ".join(features)
        assert "pyo3/extension-module" in joined, (
            "maturin features must include pyo3/extension-module so the .so "
            "resolves CPython symbols at runtime instead of linking libpython"
        )

    def test_python_source_dir(self, pyproject: dict) -> None:
        assert pyproject["tool"]["maturin"]["python-source"] == "python"

    def test_module_name(self, pyproject: dict) -> None:
        assert pyproject["tool"]["maturin"]["module-name"] == "ferrum._native"

    def test_manifest_path_points_to_pyo3_crate(self, pyproject: dict) -> None:
        manifest = pyproject["tool"]["maturin"]["manifest-path"]
        assert manifest == "crates/ferrum-pyo3/Cargo.toml", (
            f"manifest-path must point to the PyO3 crate, got {manifest!r}"
        )

    def test_sdist_includes_license(self, pyproject: dict) -> None:
        include = pyproject["tool"]["maturin"].get("include", [])
        assert "LICENSE" in include, (
            "maturin include must carry LICENSE so PyPI metadata 2.4 records "
            "License-File and does not reject the sdist"
        )


class TestVersionSync:
    """__version__ in ferrum must match pyproject.toml (single source of truth)."""

    def test_init_version_matches_pyproject(self, pyproject: dict) -> None:
        pyproject_version = pyproject["project"]["version"]
        assert ferrum.__version__ == pyproject_version, (
            f"ferrum.__version__={ferrum.__version__!r} != "
            f"pyproject.toml version={pyproject_version!r}"
        )

    def test_version_is_alpha_zero_x(self, pyproject: dict) -> None:
        v = pyproject["project"]["version"]
        assert v.startswith("0.1."), f"v0.1 must be 0.1.x until the first stable release, got {v!r}"


class TestLicense:
    """License declaration consistency."""

    def test_license_declared(self, pyproject: dict) -> None:
        license_field = pyproject["project"]["license"]
        assert "Apache-2.0" in str(license_field), (
            f"license must be Apache-2.0, got {license_field!r}"
        )

    def test_license_classifier_matches(self, pyproject: dict) -> None:
        classifiers = pyproject["project"]["classifiers"]
        assert "License :: OSI Approved :: Apache Software License" in classifiers


class TestDevDeps:
    """Dev extras must include the supply-chain and build tooling."""

    def test_dev_includes_maturin(self, pyproject: dict) -> None:
        dev_deps = pyproject["project"]["optional-dependencies"]["dev"]
        assert any(d.startswith("maturin") for d in dev_deps), "dev must include maturin"

    def test_dev_includes_pip_audit(self, pyproject: dict) -> None:
        dev_deps = pyproject["project"]["optional-dependencies"]["dev"]
        assert any("pip-audit" in d for d in dev_deps), (
            "dev must include pip-audit for Python supply-chain scanning"
        )

    def test_dev_includes_ruff_and_ty(self, pyproject: dict) -> None:
        dev_deps = pyproject["project"]["optional-dependencies"]["dev"]
        assert any(d.startswith("ruff") for d in dev_deps), "dev must include ruff"
        assert any(d.startswith("ty") for d in dev_deps), "dev must include ty"
