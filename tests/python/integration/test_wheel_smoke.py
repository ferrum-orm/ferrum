"""Integration smoke tests for the installed wheel artifact.

These tests run in CI after installing the built abi3 wheel (NOT the dev
``maturin develop`` editable install) to verify the distributed artifact is
correctly assembled and does not carry source-compilation remnants.

Marked ``@pytest.mark.integration`` so they only run when the integration
suite is explicitly selected (``-m integration``). In a dev/editable install
they skip because the wheel-context guard detects the editable path.

The tests prove:

- ``ferrum._native`` is a compiled binary extension (.so / .pyd / .dylib),
  not a Python source stub — i.e. the wheel shipped the compiled artifact.
- The install location is a normal site-packages directory, not a ``.pth``
  editable / ``__editable__`` path that would indicate a dev install
  masquerading as a wheel install.
- No ``Cargo.toml`` or ``.rs`` source files are present in the install tree
  (would indicate an sdist/source install instead of a wheel install).
- The installed ``__version__`` matches the metadata declared in the wheel.
- The native extension exposes the PyO3 boundary contract symbols
  (compile_query / hydrate_rows / the three exception classes).
"""

from __future__ import annotations

import importlib.metadata
import importlib.util
import pathlib
import sys

import pytest

import ferrum

# Skip the whole module when the Rust extension has not been built / installed.
_native = pytest.importorskip(
    "ferrum._native",
    reason="ferrum._native not available — install the built wheel first",
)


def _ferrum_package_dir() -> pathlib.Path:
    """Return the filesystem directory the ``ferrum`` package was loaded from."""
    spec = importlib.util.find_spec("ferrum")
    assert spec is not None, "ferrum must be importable"
    origin = spec.origin
    assert origin is not None, "ferrum has no origin (namespace package?)"
    return pathlib.Path(origin).resolve().parent


def _native_extension_path() -> pathlib.Path:
    """Return the path to the compiled ``ferrum._native`` extension module."""
    spec = importlib.util.find_spec("ferrum._native")
    assert spec is not None, "ferrum._native must be importable"
    origin = spec.origin
    assert origin is not None, "ferrum._native has no origin"
    return pathlib.Path(origin).resolve()


def _is_editable_install(pkg_dir: pathlib.Path) -> bool:
    """Detect editable / dev installs (maturin develop, pip -e, __editable__).

    A wheel install lives in a real site-packages directory. An editable
    install points back at the source tree (python/ferrum/) or uses a
    ``__editable__`` finder. We detect both so the wheel-context guard can
    skip cleanly in dev without false-positive failures.
    """
    pkg_dir_str = str(pkg_dir)
    if "__editable__" in pkg_dir_str:
        return True
    # maturin develop installs the .so into python/ferrum/ and adds the
    # python/ dir to the path via a .pth file — the package dir is the repo.
    if pkg_dir.parent.name == "python" and (pkg_dir.parent.parent / "pyproject.toml").exists():
        return True
    # pip -e installs use a __editable__.*.pth finder; check sys.path for the
    # source root.
    for entry in sys.path:
        if (
            pathlib.Path(entry).resolve() == pkg_dir.parent
            and (pkg_dir.parent / "pyproject.toml").exists()
        ):
            return True
    return False


@pytest.fixture(autouse=True)
def require_wheel_context():
    """Skip tests when ferrum is installed as an editable/dev build.

    These tests assert properties of a *wheel* install. In dev mode
    (``maturin develop``) the package dir is the repo source tree, which
    legitimately contains ``Cargo.toml`` / ``.rs`` files and a ``.pth`` path
    — those are not defects in a wheel install.
    """
    pkg_dir = _ferrum_package_dir()
    if _is_editable_install(pkg_dir):
        pytest.skip("ferrum is an editable/dev install — wheel smoke tests require a wheel install")


@pytest.mark.integration
class TestWheelArtifact:
    """Post-install smoke checks on the built ferrum wheel artifact."""

    def test_native_extension_is_compiled_binary(self) -> None:
        """The wheel must ship a compiled .so/.pyd/.dylib, not a .py stub.

        If uv silently compiled from source but failed to produce a binary
        extension, or if the wheel shipped a Python stub, this test fails.
        """
        ext_path = _native_extension_path()
        assert ext_path.suffix in (".so", ".pyd", ".dylib"), (
            f"ferrum._native must be a compiled binary extension, got {ext_path} "
            f"with suffix {ext_path.suffix!r}"
        )
        # A compiled extension is a binary file (not UTF-8 text). Read the
        # first few bytes and confirm it's not a Python source file.
        with ext_path.open("rb") as f:
            header = f.read(16)
        assert not header.startswith(b"#!"), (
            f"{ext_path} looks like a Python script, not a compiled extension"
        )
        # ELF binaries start with \x7fELF; Mach-O with \xfe\xed\xfa\xce /
        # \xcf\xfa\xed\xfe; PE/COFF with b"MZ". At least one of these must
        # match (or the file is a .pyd on Windows which starts with "MZ").
        binary_signatures = (b"\x7fELF", b"\xfe\xed\xfa", b"\xcf\xfa\xed", b"MZ")
        assert any(header.startswith(sig) for sig in binary_signatures), (
            f"{ext_path} does not have a recognized binary format header: "
            f"first 16 bytes = {header!r}"
        )

    def test_install_location_is_site_packages(self) -> None:
        """The package must live in a site-packages directory, not the repo.

        This catches the case where uv/pip installed from source into the
        repo tree instead of installing the wheel into site-packages.
        """
        pkg_dir = _ferrum_package_dir()
        pkg_dir_str = str(pkg_dir)
        assert "site-packages" in pkg_dir_str or "dist-packages" in pkg_dir_str, (
            f"ferrum package dir {pkg_dir} is not in site-packages — "
            "looks like a source/editable install, not a wheel install"
        )

    def test_no_source_files_in_install_tree(self) -> None:
        """The install tree must not contain Cargo.toml or .rs source files.

        A wheel install ships the compiled .so and Python source, NOT the
        Rust source. Finding Cargo.toml or .rs files indicates an sdist /
        source install leaked into the install tree.
        """
        pkg_dir = _ferrum_package_dir()
        cargo_files = list(pkg_dir.rglob("Cargo.toml"))
        rust_files = list(pkg_dir.rglob("*.rs"))
        assert not cargo_files, (
            f"Cargo.toml found in install tree — source install leaked: {cargo_files}"
        )
        assert not rust_files, (
            f".rs files found in install tree — source install leaked: {rust_files}"
        )

    def test_installed_version_matches_metadata(self) -> None:
        """``ferrum.__version__`` must match the installed distribution metadata."""
        metadata_version = importlib.metadata.version("ferrum-orm")
        assert ferrum.__version__ == metadata_version, (
            f"ferrum.__version__={ferrum.__version__!r} != "
            f"installed metadata version={metadata_version!r}"
        )

    def test_native_symbols_exposed(self) -> None:
        """The compiled extension exposes the PyO3 boundary contract symbols."""
        assert callable(_native.compile_query), "compile_query must be callable"
        assert callable(_native.hydrate_rows), "hydrate_rows must be callable"
        assert issubclass(_native.FerrumCompileError, Exception)
        assert issubclass(_native.FerrumHydrationError, Exception)
        assert issubclass(_native.FerrumInternalError, Exception)
