"""Models used by the migrations example."""

from __future__ import annotations

from ferrum import Model


class Note(Model):
    id: int = 0
    body: str = ""
