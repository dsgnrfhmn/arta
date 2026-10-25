"""A set (Portfolio or CV): a named, saveable, archivable list with undo.

The list itself is the kind's own JSON (`content`); this record does not know
what is inside. See sets/store.py for the shared behaviour and
sets/portfolio_sets.py for what a portfolio's content means.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

KINDS = ("portfolio", "cv")
NAME_PREFIX = {"portfolio": "Portfolio", "cv": "CV"}
UNDO_LIMIT = 20
NAME_MAX = 120


@dataclass(frozen=True)
class SetRecord:
    id: str
    kind: str
    name: str
    saved: bool
    saved_at: str | None
    archived: bool
    archived_at: str | None
    opportunity_id: str | None
    content: list[Any]
    created_at: str
    can_undo: bool = False
    can_redo: bool = False
    opportunity_applied: bool = False

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "SetRecord":
        keys = row.keys()
        return cls(
            id=row["id"],
            kind=row["kind"],
            name=row["name"],
            saved=bool(row["saved"]),
            saved_at=row["saved_at"],
            archived=bool(row["archived"]),
            archived_at=row["archived_at"],
            opportunity_id=row["opportunity_id"],
            content=json.loads(row["content"]),
            created_at=row["created_at"],
            can_undo=bool(row["can_undo"]) if "can_undo" in keys else False,
            can_redo=bool(row["can_redo"]) if "can_redo" in keys else False,
            opportunity_applied=bool(row["opportunity_applied"]) if "opportunity_applied" in keys else False,
        )

    def summary_api(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "name": self.name,
            "saved": self.saved,
            "saved_at": self.saved_at,
            "archived": self.archived,
            "archived_at": self.archived_at,
            "opportunity_id": self.opportunity_id,
            "can_undo": self.can_undo,
            "can_redo": self.can_redo,
        }
