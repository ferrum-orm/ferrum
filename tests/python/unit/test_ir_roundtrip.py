"""Round-trip fixture tests for the ADR-002 v1 IR wire format.

Builds QuerySet IR dicts, serializes them to JSON, deserializes, and asserts
on shape correctness. This verifies the JSON structure is stable and matches
the contract in `.cursor/plans/adr-002-ir-contract.plan.md` before the Rust
extension is available in CI.
"""

from __future__ import annotations

import json

import ferrum
from ferrum.queryset import QuerySet


class User(ferrum.Model):
    id: int = 0
    email: str = ""
    active: bool = True


class TestIrJsonRoundtrip:
    def _roundtrip(self, qs: QuerySet[User]) -> dict:  # type: ignore[type-arg]
        ir_json = qs.to_ir_json()
        assert isinstance(ir_json, str)
        ir = json.loads(ir_json)
        assert isinstance(ir, dict)
        return ir

    def test_bare_select_roundtrip(self) -> None:
        qs: QuerySet[User] = QuerySet(User)
        ir = self._roundtrip(qs)

        assert ir["version"] == 2
        assert ir["model_name"] == "User"
        assert ir["operation"]["kind"] == "select"
        assert isinstance(ir["filters"], list)
        assert isinstance(ir["order_by"], list)
        assert ir["limit"] is None
        assert ir["offset"] is None

    def test_filter_roundtrip_bind_value_shape(self) -> None:
        qs = QuerySet(User).filter(email="test@example.com")
        ir = self._roundtrip(qs)

        flt = ir["predicate"]["filter"]
        assert flt["field"]["name"] == "email"
        assert isinstance(flt["field"]["index"], int)
        assert flt["operator"] == "eq"
        # Adjacent-tag BindValue shape
        assert flt["value"] == {"type": "text", "value": "test@example.com"}

    def test_null_bind_value_has_no_value_key(self) -> None:
        qs = QuerySet(User)
        qs._filters.append({"field": "email", "operator": "is_null", "value": None})
        qs._is_filtered = True
        ir = self._roundtrip(qs)

        flt = ir["filters"][0]
        assert flt["value"] == {"type": "null"}
        assert "value" not in flt["value"]

    def test_int_bind_value_roundtrip(self) -> None:
        qs = QuerySet(User).filter(id__gt=10)
        ir = self._roundtrip(qs)

        flt = ir["predicate"]["filter"]
        assert flt["value"] == {"type": "int", "value": 10}

    def test_bool_bind_value_roundtrip(self) -> None:
        qs = QuerySet(User).filter(active=True)
        ir = self._roundtrip(qs)

        flt = ir["predicate"]["filter"]
        assert flt["value"] == {"type": "bool", "value": True}

    def test_order_by_roundtrip(self) -> None:
        qs = QuerySet(User).order_by("-id")
        ir = self._roundtrip(qs)

        assert len(ir["order_by"]) == 1
        ob = ir["order_by"][0]
        assert ob["field"]["name"] == "id"
        assert ob["direction"] == "desc"

    def test_limit_offset_roundtrip(self) -> None:
        qs = QuerySet(User).limit(25).offset(50)
        ir = self._roundtrip(qs)

        assert ir["limit"] == 25
        assert ir["offset"] == 50

    def test_full_query_roundtrip(self) -> None:
        """Full IR with filter + order + limit/offset serialises and deserialises correctly."""
        qs = (
            QuerySet(User)
            .filter(email="user@example.com")
            .filter(active=True)
            .order_by("id")
            .limit(10)
            .offset(0)
        )
        ir = self._roundtrip(qs)

        assert ir["version"] == 2
        assert ir["model_name"] == "User"
        assert ir["predicate"]["kind"] == "and"
        assert len(ir["predicate"]["children"]) == 2
        assert len(ir["order_by"]) == 1
        assert ir["limit"] == 10
        assert ir["offset"] == 0

        # Every filter value must be an adjacent-tagged dict
        for child in ir["predicate"]["children"]:
            flt = child["filter"]
            assert "type" in flt["value"]

        # Field refs must carry both name and index
        for child in ir["predicate"]["children"]:
            flt = child["filter"]
            assert "name" in flt["field"]
            assert "index" in flt["field"]

    def test_select_fields_carry_index_and_name(self) -> None:
        qs: QuerySet[User] = QuerySet(User)
        ir = self._roundtrip(qs)

        for field in ir["operation"]["fields"]:
            assert "name" in field
            assert "index" in field
            assert isinstance(field["index"], int)


class TestMetadataJsonShape:
    def test_to_metadata_json_is_valid_json(self) -> None:
        meta = User.get_metadata()
        raw = meta.to_metadata_json()
        assert isinstance(raw, str)
        parsed = json.loads(raw)
        assert isinstance(parsed, dict)

    def test_metadata_json_top_level_keys(self) -> None:
        meta = User.get_metadata()
        parsed = json.loads(meta.to_metadata_json())

        assert parsed["model_name"] == "User"
        assert parsed["table_name"] == "user"
        assert isinstance(parsed["pk_index"], int)
        assert isinstance(parsed["fields"], list)

    def test_metadata_json_field_shape(self) -> None:
        meta = User.get_metadata()
        parsed = json.loads(meta.to_metadata_json())

        for field in parsed["fields"]:
            assert "name" in field
            assert "column_name" in field
            assert "field_type" in field
            assert "allowed_operators" in field
            assert "nullable" in field
            # pk is Python-internal and must NOT cross the boundary
            assert "pk" not in field

    def test_metadata_json_field_types_are_strings(self) -> None:
        meta = User.get_metadata()
        parsed = json.loads(meta.to_metadata_json())

        for field in parsed["fields"]:
            assert isinstance(field["field_type"], str)
            assert isinstance(field["allowed_operators"], list)
            assert all(isinstance(op, str) for op in field["allowed_operators"])

    def test_metadata_json_operators_use_eq_token(self) -> None:
        meta = User.get_metadata()
        parsed = json.loads(meta.to_metadata_json())

        id_field = next(f for f in parsed["fields"] if f["name"] == "id")
        assert "eq" in id_field["allowed_operators"]
        assert "exact" not in id_field["allowed_operators"]

    def test_metadata_json_operators_use_is_null_token(self) -> None:
        meta = User.get_metadata()
        parsed = json.loads(meta.to_metadata_json())

        id_field = next(f for f in parsed["fields"] if f["name"] == "id")
        assert "is_null" in id_field["allowed_operators"]
        assert "isnull" not in id_field["allowed_operators"]
