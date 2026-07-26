"""Unit tests for ModelMetadata builder and ModelConfig factory.

Covers DM-1 (allowlist derivation at class-definition time) and
DM-3 (unsupported type raises at definition) requirements.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime
from typing import Annotated, Any, cast
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest

import ferrum
from ferrum.models import _to_snake_case
from ferrum.queryset import QuerySet


class TestSnakeCase:
    def test_simple(self) -> None:
        assert _to_snake_case("User") == "user"

    def test_camel(self) -> None:
        assert _to_snake_case("UserProfile") == "user_profile"

    def test_multi_word(self) -> None:
        assert _to_snake_case("BlogPost") == "blog_post"

    def test_already_snake(self) -> None:
        assert _to_snake_case("user_profile") == "user_profile"

    def test_acronym(self) -> None:
        # Standard regex-based conversion treats "HTTPS" as one word
        assert _to_snake_case("HTTPSRequest") == "https_request"


class SimpleUser(ferrum.Model):
    id: int = 0
    email: str = ""
    active: bool = True


class TestModelMetadataDerivation:
    def test_metadata_is_built(self) -> None:
        assert SimpleUser.__ferrum_metadata__ is not None

    def test_model_name(self) -> None:
        meta = SimpleUser.get_metadata()
        assert meta.model_name == "SimpleUser"

    def test_table_name_defaults_to_snake_case(self) -> None:
        meta = SimpleUser.get_metadata()
        assert meta.table_name == "simple_user"

    def test_fields_ordered_matches_class_definition(self) -> None:
        meta = SimpleUser.get_metadata()
        names = [f.name for f in meta.fields]
        assert names == ["id", "email", "active"]

    def test_id_field_is_pk(self) -> None:
        meta = SimpleUser.get_metadata()
        id_field = meta.fields[0]
        assert id_field.name == "id"
        assert id_field.pk is True
        assert meta.pk_index == 0

    def test_id_field_db_type_is_big_int(self) -> None:
        meta = SimpleUser.get_metadata()
        id_field = meta.fields[0]
        assert id_field.field_type == "big_int"

    def test_email_field(self) -> None:
        meta = SimpleUser.get_metadata()
        email_field = next(f for f in meta.fields if f.name == "email")
        assert email_field.field_type == "text"
        assert email_field.nullable is False
        assert email_field.pk is False

    def test_bool_field(self) -> None:
        meta = SimpleUser.get_metadata()
        active_field = next(f for f in meta.fields if f.name == "active")
        assert active_field.field_type == "bool"
        assert active_field.nullable is False

    def test_allowed_sort_directions(self) -> None:
        meta = SimpleUser.get_metadata()
        assert "asc" in meta.allowed_sort_directions
        assert "desc" in meta.allowed_sort_directions

    def test_metadata_is_frozen(self) -> None:
        meta = SimpleUser.get_metadata()
        with pytest.raises(dataclasses.FrozenInstanceError):
            meta.table_name = "hacked"  # type: ignore[misc]

    def test_field_meta_is_frozen(self) -> None:
        meta = SimpleUser.get_metadata()
        with pytest.raises(dataclasses.FrozenInstanceError):
            meta.fields[0].name = "hacked"  # type: ignore[misc]


class TestModelConfigTableOverride:
    def test_model_config_overrides_table_name(self) -> None:
        class Post(ferrum.Model):
            model_config = ferrum.ModelConfig(table="blog_posts")
            id: int = 0
            title: str = ""

        assert Post.__ferrum_table__ == "blog_posts"
        assert Post.get_metadata().table_name == "blog_posts"

    def test_inner_meta_overrides_table_name(self) -> None:
        class Comment(ferrum.Model):
            id: int = 0
            body: str = ""

            class Meta:
                table = "post_comments"

        assert Comment.__ferrum_table__ == "post_comments"
        assert Comment.get_metadata().table_name == "post_comments"

    def test_inner_meta_takes_precedence_over_model_config(self) -> None:
        class Article(ferrum.Model):
            model_config = ferrum.ModelConfig(table="from_config")
            id: int = 0

            class Meta:
                table = "from_meta"

        assert Article.get_metadata().table_name == "from_meta"


class TestNullableFields:
    def test_optional_field_is_nullable(self) -> None:
        class WithOptional(ferrum.Model):
            id: int = 0
            bio: str | None = None

        meta = WithOptional.get_metadata()
        bio_field = next(f for f in meta.fields if f.name == "bio")
        assert bio_field.nullable is True
        assert bio_field.field_type == "text"

    def test_non_optional_field_is_not_nullable(self) -> None:
        meta = SimpleUser.get_metadata()
        email_field = next(f for f in meta.fields if f.name == "email")
        assert email_field.nullable is False


class TestAllowlistDerivation:
    def test_text_field_operators(self) -> None:
        meta = SimpleUser.get_metadata()
        email_field = next(f for f in meta.fields if f.name == "email")
        ops = email_field.allowed_operators
        assert "eq" in ops
        assert "contains" in ops
        assert "icontains" in ops
        assert "startswith" in ops
        assert "is_null" in ops
        # Text fields do NOT have numeric operators
        assert "gt" not in ops

    def test_int_field_operators(self) -> None:
        meta = SimpleUser.get_metadata()
        id_field = next(f for f in meta.fields if f.name == "id")
        ops = id_field.allowed_operators
        assert "eq" in ops
        assert "gt" in ops
        assert "gte" in ops
        assert "lt" in ops
        assert "lte" in ops
        assert "in" in ops
        assert "is_null" in ops
        # Integer fields do NOT have text operators
        assert "contains" not in ops

    def test_bool_field_operators(self) -> None:
        meta = SimpleUser.get_metadata()
        active_field = next(f for f in meta.fields if f.name == "active")
        ops = active_field.allowed_operators
        assert "eq" in ops
        assert "is_null" in ops
        # Bool fields have limited operator set
        assert "gt" not in ops
        assert "contains" not in ops


class TestMultipleFieldTypes:
    def test_various_scalar_types(self) -> None:
        class AllTypes(ferrum.Model):
            id: int = 0
            name: str = ""
            score: float = 0.0
            uid: UUID = UUID("00000000-0000-0000-0000-000000000000")
            created: datetime = datetime(2024, 1, 1)
            data: bytes = b""

        meta = AllTypes.get_metadata()
        type_map = {f.name: f.field_type for f in meta.fields}
        assert type_map["name"] == "text"
        assert type_map["score"] == "float"
        assert type_map["uid"] == "uuid"
        assert type_map["created"] == "datetime"
        assert type_map["data"] == "bytes"


class TestGetMetadataErrors:
    def test_base_model_has_no_metadata(self) -> None:
        with pytest.raises(AttributeError, match="no metadata"):
            ferrum.Model.get_metadata()


class TestFieldWithExtras:
    """Verify that Field() extras flow correctly through _build_metadata to FieldMeta."""

    def test_varchar_field_has_correct_sql_type(self) -> None:
        class MfVarchar(ferrum.Model):
            id: int
            name: Annotated[str, ferrum.Field(max_length=100)]

        field_meta = next(f for f in MfVarchar.get_metadata().fields if f.name == "name")
        assert field_meta.max_length == 100
        assert field_meta.sql_type == "VARCHAR(100)"

    def test_db_column_override(self) -> None:
        class MfColOverride(ferrum.Model):
            id: int
            email: Annotated[str, ferrum.Field(db_column="user_email")]

        field_meta = next(f for f in MfColOverride.get_metadata().fields if f.name == "email")
        assert field_meta.column_name == "user_email"
        # The field name (Python attribute) is unchanged.
        assert field_meta.name == "email"

    def test_unique_flag(self) -> None:
        class MfUnique(ferrum.Model):
            id: int
            handle: Annotated[str, ferrum.Field(unique=True)]

        field_meta = next(f for f in MfUnique.get_metadata().fields if f.name == "handle")
        assert field_meta.unique is True

    def test_db_index_flag(self) -> None:
        class MfIndexed(ferrum.Model):
            id: int
            slug: Annotated[str, ferrum.Field(db_index=True)]

        field_meta = next(f for f in MfIndexed.get_metadata().fields if f.name == "slug")
        assert field_meta.db_index is True


class TestSchemaFidelityMetadata:
    def test_jsonb_list_is_distinct_from_postgres_array(self) -> None:
        class JsonListModel(ferrum.Model):
            id: int
            labels: list[str] = ferrum.Field(default_factory=list, jsonb_list=True)
            tag_ids: list[UUID] = ferrum.Field(default_factory=list)

        metadata = JsonListModel.get_metadata()
        labels = next(field for field in metadata.fields if field.name == "labels")
        tag_ids = next(field for field in metadata.fields if field.name == "tag_ids")

        assert labels.field_type == "json"
        assert labels.sql_type == "JSONB"
        assert labels.jsonb_list is True
        assert tag_ids.field_type == "array_uuid"
        assert tag_ids.sql_type == "UUID[]"
        assert tag_ids.jsonb_list is False

    def test_jsonb_list_requires_list_annotation(self) -> None:
        with pytest.raises(TypeError, match="requires a list annotation"):

            class InvalidJsonList(ferrum.Model):
                id: int
                labels: str = ferrum.Field(jsonb_list=True)

    def test_array_rejects_unsupported_element_type(self) -> None:
        with pytest.raises(TypeError, match="unsupported PostgreSQL ARRAY element type"):

            class InvalidArray(ferrum.Model):
                id: int
                values: list[dict[str, str]]

    def test_array_rejects_nullable_elements(self) -> None:
        with pytest.raises(TypeError, match="must be non-nullable scalar values"):

            class InvalidNullableArray(ferrum.Model):
                id: int
                values: list[str | None]

    def test_generated_and_read_only_fields_are_readable_but_not_writable(self) -> None:
        class GeneratedModel(ferrum.Model):
            id: UUID = ferrum.Field(primary_key=True, generated=True)
            source: str
            computed: str = ferrum.Field(read_only=True)

        metadata = GeneratedModel.get_metadata()
        assert [field.name for field in metadata.fields] == ["id", "source", "computed"]
        assert metadata.writable_field_names == frozenset({"source"})
        assert next(field for field in metadata.fields if field.name == "id").generated is True
        computed = next(field for field in metadata.fields if field.name == "computed")
        assert computed.read_only is True

        write_payload = metadata.to_write_metadata_dict()
        assert [field["name"] for field in write_payload["fields"]] == ["source"]
        assert write_payload["pk_fields"] == []

    def test_read_metadata_keeps_schema_fidelity_flags(self) -> None:
        class MetadataFlags(ferrum.Model):
            id: int
            values: list[str] = ferrum.Field(jsonb_list=True, read_only=True)

        field_payload = MetadataFlags.get_metadata().to_metadata_dict()["fields"][1]
        assert field_payload["jsonb_list"] is True
        assert field_payload["read_only"] is True


class TestObjectsManager:
    """Verify the Manager descriptor on Model subclasses (DX blocker B-1)."""

    def test_objects_returns_queryset_instance(self) -> None:
        assert isinstance(SimpleUser.objects, ferrum.QuerySet)

    def test_objects_is_bound_to_model_class(self) -> None:
        qs = SimpleUser.objects
        assert qs._model is SimpleUser

    def test_objects_returns_fresh_queryset_each_access(self) -> None:
        qs1 = SimpleUser.objects
        qs2 = SimpleUser.objects
        assert qs1 is not qs2

    def test_objects_works_for_any_model_subclass(self) -> None:
        class Article(ferrum.Model):
            id: int = 0
            title: str = ""

        qs = Article.objects
        assert isinstance(qs, ferrum.QuerySet)
        assert qs._model is Article

    def test_objects_raises_on_instance_access(self) -> None:
        user = SimpleUser()
        with pytest.raises(AttributeError, match="class-level manager"):
            _ = user.objects  # type: ignore[attr-defined]


class WritableModel(ferrum.Model):
    id: int = ferrum.Field(default=0, primary_key=True, generated=True)
    source: str = ""
    computed: str = ferrum.Field(default="", read_only=True)


class TestWriteMetadataEnforcement:
    def test_reads_keep_generated_fields_but_writes_reject_them(self) -> None:
        queryset = QuerySet(WritableModel)

        select_names = [field["name"] for field in queryset._build_ir()["operation"]["fields"]]
        insert_names = [
            field["name"]
            for field, _value in queryset._build_insert_ir({"source": "input"})["operation"][
                "values"
            ]
        ]

        assert select_names == ["id", "source", "computed"]
        assert insert_names == ["source"]
        with pytest.raises(ferrum.FerrumCompileError, match="generated/read-only"):
            queryset._build_insert_ir({"computed": "forbidden"})
        with pytest.raises(ferrum.FerrumCompileError, match="generated/read-only"):
            queryset._build_bulk_insert_ir(
                [{"source": "input", "computed": "forbidden"}],
                returning=True,
            )
        with pytest.raises(ferrum.FerrumCompileError, match="generated/read-only"):
            queryset._build_upsert_sql(
                WritableModel.get_metadata(),
                {"source": "input", "computed": "forbidden"},
                conflict_fields=["source"],
                update_fields=None,
                returning=True,
            )

    def test_model_instances_omit_generated_fields_for_insert_and_upsert(self) -> None:
        queryset = QuerySet(WritableModel)
        obj = WritableModel(id=9, source="input", computed="database")

        row = queryset._object_to_insert_row_dict(obj, WritableModel.get_metadata())
        sql, bound = queryset._build_upsert_sql(
            WritableModel.get_metadata(),
            row,
            conflict_fields=["source"],
            update_fields=None,
            returning=True,
        )
        write_sql, returning_sql = sql.split(" RETURNING ", maxsplit=1)

        assert row == {"source": "input"}
        assert '"id"' not in write_sql
        assert '"computed"' not in write_sql
        assert '"id"' in returning_sql
        assert '"computed"' in returning_sql
        assert bound == ["input"]

    def test_update_and_bulk_update_reject_read_only_fields(self) -> None:
        queryset = QuerySet(WritableModel).filter(id=1)

        with pytest.raises(ferrum.FerrumCompileError, match="generated/read-only"):
            queryset._build_update_ir({"computed": "forbidden"})
        with pytest.raises(ferrum.FerrumCompileError, match="generated/read-only"):
            queryset._build_bulk_update_ir(
                [(1, {"computed": "forbidden"})],
                ["computed"],
            )

    @pytest.mark.asyncio
    async def test_update_instance_default_assignments_use_writable_fields(self) -> None:
        obj = WritableModel(id=7, source="changed", computed="database")
        update = AsyncMock(return_value=1)

        with patch.object(QuerySet, "update", update):
            count = await QuerySet(WritableModel).update_instance(cast(Any, object()), obj)

        assert count == 1
        assert update.await_args is not None
        assert update.await_args.kwargs == {"source": "changed"}
