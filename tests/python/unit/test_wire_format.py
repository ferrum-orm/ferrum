"""Unit tests for the optional MessagePack wire format.

Covers ``resolve_wire_format`` env/config resolution, the missing-package guard,
and JSON↔MessagePack equivalence at the native boundary: ``compile_query`` and
``compile_query_msgpack`` must emit identical SQL and decode to identical bound
parameters for the same metadata + IR.
"""

from __future__ import annotations

import importlib

import pytest

import ferrum
from ferrum.config import DEFAULT_WIRE_FORMAT, WIRE_FORMAT_ENV, resolve_wire_format
from ferrum.errors import FerrumConfigError
from ferrum.queryset import QuerySet, _decode_bound_param

msgpack = pytest.importorskip("msgpack")
_native = pytest.importorskip("ferrum._native")


class _WireModel(ferrum.Model):
    id: int = 0
    email: str = ""
    active: bool = True


# ---------------------------------------------------------------------------
# resolve_wire_format
# ---------------------------------------------------------------------------


def test_resolve_wire_format_env_msgpack(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(WIRE_FORMAT_ENV, "msgpack")
    assert resolve_wire_format() == "msgpack"


def test_resolve_wire_format_env_case_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(WIRE_FORMAT_ENV, "  MsgPack ")
    assert resolve_wire_format() == "msgpack"


def test_resolve_wire_format_env_invalid_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(WIRE_FORMAT_ENV, "protobuf")
    assert resolve_wire_format() == DEFAULT_WIRE_FORMAT


# ---------------------------------------------------------------------------
# Missing-package guard
# ---------------------------------------------------------------------------


def test_require_msgpack_raises_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    import ferrum.queryset as qs_mod

    monkeypatch.setattr(qs_mod, "_msgpack_mod", None)

    real_import = importlib.import_module

    def _fake_import(name: str, package: str | None = None) -> object:
        if name == "msgpack":
            raise ImportError("no msgpack")
        return real_import(name, package)

    monkeypatch.setattr(qs_mod.importlib, "import_module", _fake_import)
    with pytest.raises(FerrumConfigError):
        qs_mod._require_msgpack()


# ---------------------------------------------------------------------------
# JSON ↔ MessagePack equivalence at the native boundary
# ---------------------------------------------------------------------------


def _decoded_params(compiled: dict) -> list[object]:
    return [_decode_bound_param(p) for p in compiled["bound_params"]]


@pytest.mark.parametrize("dialect", ["postgres", "mysql", "sqlite", "mssql"])
def test_compile_json_matches_msgpack(dialect: str) -> None:
    qs = QuerySet(_WireModel).filter(email="a@example.com").filter(active=True).limit(5).offset(10)
    ir = qs._build_ir()
    meta = _WireModel.get_metadata()

    json_compiled = _native.compile_query(meta.to_metadata_json(), qs.to_ir_json(), dialect)

    meta_mp = msgpack.packb(meta.to_metadata_dict(), use_bin_type=True)
    ir_mp = msgpack.packb(ir, use_bin_type=True)
    mp_compiled = _native.compile_query_msgpack(meta_mp, ir_mp, dialect)
    mp_compiled["bound_params"] = msgpack.unpackb(mp_compiled["bound_params"], raw=False)

    assert json_compiled["sql_text"] == mp_compiled["sql_text"]
    assert _decoded_params(json_compiled) == _decoded_params(mp_compiled)


class _FtsWireModel(ferrum.Model):
    id: int = 0
    search_vector: ferrum.TSVector | None = None


@pytest.mark.parametrize("dialect", ["postgres", "mysql", "sqlite", "mssql"])
def test_text_rank_by_json_matches_msgpack(dialect: str) -> None:
    qs = (
        QuerySet(_FtsWireModel)
        .filter(search_vector__match="hello")
        .rank_by("search_vector", "hello", mode="plain")
        .limit(3)
    )
    ir = qs._build_ir()
    meta = _FtsWireModel.get_metadata()

    json_compiled = _native.compile_query(meta.to_metadata_json(), qs.to_ir_json(), dialect)
    meta_mp = msgpack.packb(meta.to_metadata_dict(), use_bin_type=True)
    ir_mp = msgpack.packb(ir, use_bin_type=True)
    mp_compiled = _native.compile_query_msgpack(meta_mp, ir_mp, dialect)
    mp_compiled["bound_params"] = msgpack.unpackb(mp_compiled["bound_params"], raw=False)

    assert json_compiled["sql_text"] == mp_compiled["sql_text"]
    assert _decoded_params(json_compiled) == _decoded_params(mp_compiled)
    assert "text_rank_by" in ir


def test_hydrate_json_matches_msgpack() -> None:
    meta = _WireModel.get_metadata()
    rows = [
        {"id": 1, "email": "a@example.com", "active": True},
        {"id": 2, "email": "b@example.com", "active": False},
    ]
    import json as _json

    json_hydrated = _native.hydrate_rows(meta.to_metadata_json(), _json.dumps(rows))

    meta_mp = msgpack.packb(meta.to_metadata_dict(), use_bin_type=True)
    rows_mp = msgpack.packb(rows, use_bin_type=True)
    mp_hydrated = _native.hydrate_rows_msgpack(meta_mp, rows_mp)

    assert json_hydrated == mp_hydrated
