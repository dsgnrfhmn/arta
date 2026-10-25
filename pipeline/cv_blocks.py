"""Import a master CV into addressable blocks with stable ids.

A block's id is a UUID assigned once, here, and carried from then on. It is never
derived from text (an edit would change identity) and never positional (a reorder
would). Re-importing an edited master merges by text: an exact match keeps its id,
new text gets a new id, and blocks that disappeared are *reported*, never deleted
from the sets that already hold them (a set keeps its own copy of the text).

The input is a neutral list of entries, so the format of the master (Figma today, a
docx or Google Doc later) is a separate parser in front of this function.
"""

from __future__ import annotations

import uuid
from collections import defaultdict, deque
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass

from core import sanitize
from sources.cv_source import BLOCK_KINDS, Block

TEXT_MAX = 20_000


class CvImportError(ValueError):
    """The master has an entry that cannot become a block. The message names it."""


@dataclass(frozen=True)
class Entry:
    kind: str
    text: str


@dataclass(frozen=True)
class ImportResult:
    blocks: list[Block]    # the new master, in master order
    kept: list[Block]      # text matched an existing block, id carried over
    added: list[Block]     # new text, new id
    removed: list[Block]   # existing blocks whose text is no longer in the master


def _clean(entries: Iterable[Entry]) -> list[Entry]:
    out: list[Entry] = []
    for n, e in enumerate(entries, 1):
        if e.kind not in BLOCK_KINDS:
            raise CvImportError(f"Entry {n}: unknown block kind {e.kind!r}.")
        try:
            text = sanitize.plain_text(e.text, TEXT_MAX)
        except ValueError as exc:
            raise CvImportError(f"Entry {n}: {exc}") from None
        if not text:
            raise CvImportError(f"Entry {n}: a block cannot be empty.")
        out.append(Entry(e.kind, text))
    return out


def import_blocks(
    entries: Iterable[Entry],
    existing: Sequence[Block] = (),
    new_id: Callable[[], str] = lambda: str(uuid.uuid4()),
) -> ImportResult:
    """Merge the master's entries with the blocks already known.

    Equal texts (a year heading that appears twice) are matched in order: the first
    entry takes the first unused existing block with that text, and so on.
    """
    by_text: dict[str, deque[Block]] = defaultdict(deque)
    for b in existing:
        by_text[b.text].append(b)

    blocks: list[Block] = []
    kept: list[Block] = []
    added: list[Block] = []
    for e in _clean(entries):
        pool = by_text.get(e.text)
        if pool:
            old = pool.popleft()
            block = Block(old.id, e.kind, e.text)
            kept.append(block)
        else:
            block = Block(new_id(), e.kind, e.text)
            added.append(block)
        blocks.append(block)

    removed = [b for pool in by_text.values() for b in pool]
    order = {b.id: i for i, b in enumerate(existing)}
    removed.sort(key=lambda b: order[b.id])
    return ImportResult(blocks, kept, added, removed)
