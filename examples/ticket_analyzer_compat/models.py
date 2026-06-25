"""Ferrum models mirroring ticket-analyzer persistence patterns.

Used by the compatibility example and integration tests. Table names are prefixed
with ``ta_compat_`` so the fixture can coexist with other local databases.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, ClassVar
from uuid import UUID

import ferrum


class Team(ferrum.Model):
    model_config = ferrum.ModelConfig(table="ta_compat_teams")

    id: Annotated[UUID, ferrum.Field(primary_key=True, uuid_generate="v7")]
    name: str = ""


class Ticket(ferrum.Model):
    model_config = ferrum.ModelConfig(table="ta_compat_tickets")

    id: Annotated[int, ferrum.Field(primary_key=True)]
    first_seen_at: Annotated[datetime, ferrum.Field(primary_key=True)]
    team_id: UUID
    helpshift_id: str = ""
    summary: str = ""
    summary_embedding: Annotated[
        ferrum.Vector | None,
        ferrum.Field(vector_dimensions=8),
    ] = None


class Issue(ferrum.Model):
    model_config = ferrum.ModelConfig(table="ta_compat_issues")

    id: Annotated[UUID, ferrum.Field(primary_key=True, uuid_generate="v7")]
    team_id: UUID
    dedup_key: str = ""
    title: str = ""
    summary: str = ""

    class Meta:
        indexes: ClassVar[list[ferrum.Index]] = [
            ferrum.Index(fields=("team_id", "dedup_key"), unique=True),
        ]


class Alert(ferrum.Model):
    model_config = ferrum.ModelConfig(table="ta_compat_alerts")

    id: Annotated[UUID, ferrum.Field(primary_key=True, uuid_generate="v7")]
    team_id: UUID
    title: str = ""
    ticket_ids: list[UUID] = ferrum.Field(default_factory=list)
    slack_delivery: dict = ferrum.Field(default_factory=dict)
