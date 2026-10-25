"""The CvSource interface: where the master CV blocks come from.

Two implementations: a Google Doc on Drive (cv.source = "drive", sources/drive_cv.py)
and a master file on this machine (LocalCv: "local", or "fixtures" for the synthetic
test master). Callers hold ids from a listing, never paths.

A block is {id, kind, text}. Its id is a UUID assigned once at import and
carried from then on: never derived from the text (an edit would change it)
and never from the position (a reorder would). A referent (Short, Middle,
Full) is an ordered list of these blocks, so the same block has the same id in
every referent and in every set made from it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

BLOCK_KINDS = ("heading", "text")


@dataclass(frozen=True)
class Block:
    id: str
    kind: str
    text: str

    def to_api(self) -> dict[str, str]:
        return {"id": self.id, "kind": self.kind, "text": self.text}


@dataclass(frozen=True)
class Referent:
    id: str
    name: str

    def to_api(self) -> dict[str, str]:
        return {"id": self.id, "name": self.name, "kind": "master"}


class CvSource(Protocol):
    def list_referents(self) -> list[Referent]:
        """Short, Middle, Full, in that order."""

    def get_blocks(self, referent_id: str) -> list[Block]:
        """The referent's blocks in CV order. Raises sources.project_source.NotFound."""
