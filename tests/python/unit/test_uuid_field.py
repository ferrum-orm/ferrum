"""Unit tests for UUID primary keys and bind encoding."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID, uuid4

import ferrum
from ferrum.migrations.orchestrator import _col_def, compute_plan
from ferrum.queryset import _encode_bind_value


class TestUuidPkMetadata:
    def test_uuid_pk_auto_db_default(self) -> None:
        class UuidUser(ferrum.Model):
            id: Annotated[UUID, ferrum.Field(primary_key=True)]
            email: str

        id_field = next(f for f in UuidUser.get_metadata().fields if f.name == "id")
        assert id_field.field_type == "uuid"
        assert id_field.pk is True
        assert id_field.sql_type == "UUID"
        assert id_field.db_default == "gen_random_uuid()"

    def test_uuid_generate_v4_sets_db_default(self) -> None:
        class UuidV4(ferrum.Model):
            id: Annotated[UUID, ferrum.Field(primary_key=True, uuid_generate="v4")]
            name: str

        id_field = next(f for f in UuidV4.get_metadata().fields if f.name == "id")
        assert id_field.db_default == "gen_random_uuid()"

    def test_uuid_generate_v7_sets_db_default(self) -> None:
        class UuidV7(ferrum.Model):
            id: Annotated[UUID, ferrum.Field(primary_key=True, uuid_generate="v7")]
            name: str

        id_field = next(f for f in UuidV7.get_metadata().fields if f.name == "id")
        assert id_field.db_default == "uuid_generate_v7()"

    def test_explicit_db_default_overrides_auto_injection(self) -> None:
        class UuidExplicit(ferrum.Model):
            id: UUID = ferrum.Field(primary_key=True, default="gen_random_uuid()")
            name: str

        id_field = next(f for f in UuidExplicit.get_metadata().fields if f.name == "id")
        assert id_field.db_default == "gen_random_uuid()"


class TestUuidMigrationPlan:
    def test_compute_plan_emits_gen_random_uuid_default(self) -> None:
        class UuidPlan(ferrum.Model):
            id: Annotated[UUID, ferrum.Field(primary_key=True)]
            email: str

        plan = compute_plan([UuidPlan], existing_tables={})
        create_op = next(op for op in plan["ops"] if op["kind"] == "create_table")
        id_col = next(c for c in create_op["columns"] if c["name"] == "id")
        assert id_col["default"] == "gen_random_uuid()"
        assert id_col["sql_type"] == "UUID"

    def test_col_def_accepts_gen_random_uuid(self) -> None:
        result = _col_def({"name": "id", "sql_type": "UUID", "default": "gen_random_uuid()"})
        assert "DEFAULT gen_random_uuid()" in result


class TestUuidBindEncoding:
    def test_uuid_encodes_as_text(self) -> None:
        uid = uuid4()
        encoded = _encode_bind_value(uid)
        assert encoded == {"type": "text", "value": str(uid)}

    def test_uuid_not_stringified_via_generic_fallback(self) -> None:
        uid = UUID("12345678-1234-5678-1234-567812345678")
        encoded = _encode_bind_value(uid)
        assert encoded["type"] == "text"
        assert encoded["value"] == "12345678-1234-5678-1234-567812345678"
