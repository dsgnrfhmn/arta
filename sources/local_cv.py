"""CvSource over a master CV file on this machine: cv.source = "local" (cv.local_file,
written by scripts/build_cv_master.py) or "fixtures" (tests/fixtures/cv/master.json,
synthetic test data).

The file is validated on load and a bad one raises: a master that does not
hold together (a duplicate id, a referent naming a block that does not exist,
an id that is not a UUID) must be noticed, not served.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from core import sanitize
from sources.cv_source import BLOCK_KINDS, Block, Referent
from sources.project_source import NotFound, SourceError

TEXT_MAX = 20_000


def _uuid(raw: object) -> str:
    try:
        return str(uuid.UUID(str(raw)))
    except ValueError:
        raise SourceError(f"CV fixture: {raw!r} is not a UUID.") from None


class LocalCv:
    def __init__(self, path: Path):
        data = json.loads(Path(path).read_text("utf-8"))
        self._blocks: dict[str, Block] = {}
        for b in data["blocks"]:
            block = Block(_uuid(b["id"]), b["kind"], sanitize.plain_text(b["text"], TEXT_MAX))
            if block.kind not in BLOCK_KINDS:
                raise SourceError(f"CV fixture: unknown block kind {block.kind!r}.")
            if block.id in self._blocks:
                raise SourceError(f"CV fixture: block id {block.id} appears twice.")
            self._blocks[block.id] = block
        self._referents: dict[str, tuple[Referent, list[str]]] = {}
        for r in data["referents"]:
            ids = [_uuid(i) for i in r["blocks"]]
            missing = [i for i in ids if i not in self._blocks]
            if missing:
                raise SourceError(f"CV fixture: referent {r['id']!r} names unknown block {missing[0]}.")
            if len(set(ids)) != len(ids):
                raise SourceError(f"CV fixture: referent {r['id']!r} lists a block twice.")
            self._referents[r["id"]] = (Referent(r["id"], r["name"]), ids)

    def list_referents(self) -> list[Referent]:
        return [ref for ref, _ in self._referents.values()]

    def get_blocks(self, referent_id: str) -> list[Block]:
        if referent_id not in self._referents:
            raise NotFound(f"No such referent: {referent_id}")
        return [self._blocks[i] for i in self._referents[referent_id][1]]
