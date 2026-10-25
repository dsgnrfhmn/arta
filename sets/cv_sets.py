"""What a CV set's content means: an ordered list of blocks.

    {"id": uuid, "kind": "heading" | "text", "text": str}

A block is copied into the set from a referent with the id it has there, so
the referent pane can tell which blocks are already in the set by id. Editing
changes the text in this set only, never the referent. A block is in a set at
most once. An added block goes where it belongs, not at the end: into its own section
(created in the referent's section order if the set has none yet), newest year first. Every change goes through sets.store.mutate, so each one is an
undo step.
"""

from __future__ import annotations

import re
import sqlite3
import uuid
from typing import Any

from core import sanitize
from core.errors import Conflict, NotFound
from layout import mock_paginate
from models.set import SetRecord
from pipeline import edit_cv
from sets import store
from sources.cv_source import CvSource

KIND = "cv"
SET_REFERENT = "set:"
TEXT_MAX = 20_000


def _block_id(raw: object) -> str:
    try:
        return str(uuid.UUID(str(raw)))
    except ValueError:
        raise ValueError("block_id must be a UUID.") from None


def _text(raw: object) -> str:
    text = sanitize.plain_text(raw, TEXT_MAX)
    if not text:
        raise ValueError("A block cannot be empty. Delete it instead.")
    return text


def list_referents(conn: sqlite3.Connection, source: CvSource) -> list[dict[str, Any]]:
    """Short, Middle, Full, then the saved CV sets (spec 6.5). A set that is
    archived is not offered."""
    saved = [
        {"id": SET_REFERENT + r.id, "name": r.name, "kind": "set"}
        for r in store.list_sets(conn, KIND) if r.saved and not r.archived
    ]
    return [r.to_api() for r in source.list_referents()] + saved


def referent_blocks(conn: sqlite3.Connection, source: CvSource, referent_id: str) -> list[dict[str, str]]:
    if referent_id.startswith(SET_REFERENT):
        record = store.get(conn, KIND, referent_id[len(SET_REFERENT):])
        if record.archived:
            raise NotFound(f"No such referent: {referent_id}")
        return [dict(b) for b in record.content]
    return [b.to_api() for b in source.get_blocks(referent_id)]


_YEAR = re.compile(r"(\d{4})\t")


def _year(block: dict[str, str]) -> int | None:
    m = _YEAR.match(block["text"])
    return int(m.group(1)) if m else None


def _place(content: list[dict[str, str]], block: dict[str, str], ref: list[dict[str, str]]) -> list[dict[str, str]]:
    """`content` with `block` inserted where it belongs. Its section is the nearest heading
    above it in the referent it came from, and sections follow that referent's order. In
    a section, entries run newest year first; an entry with no year (the name line, About)
    goes last, and an entry of the same year goes after the ones already there. A block
    with no heading above it is appended, as before."""
    order: dict[str, int] = {}      # heading id -> position among the referent's headings
    section_of: dict[str, dict[str, str]] = {}
    current = None
    for b in ref:
        if b["kind"] == "heading":
            current = b
            order[b["id"]] = len(order)
        elif current is not None:
            section_of[b["id"]] = current
    head = block if block["kind"] == "heading" else section_of.get(block["id"])
    if head is None:
        return content + [block]
    head_at = next((i for i, b in enumerate(content) if b["id"] == head["id"]), None)

    if head_at is None:
        # No such section in the set yet: it goes before the first set section that follows it
        # in the referent's order, else at the end (a section the referent does not know ranks last).
        mine = order[head["id"]]
        at = next((i for i, b in enumerate(content)
                   if b["kind"] == "heading" and order.get(b["id"], len(order)) > mine), len(content))
        return content[:at] + ([head, block] if block is not head else [head]) + content[at:]
    if block is head:
        return content + [block]

    end = next((i for i in range(head_at + 1, len(content)) if content[i]["kind"] == "heading"), len(content))
    year = _year(block)
    at = end
    if year is not None:
        at = next((i for i in range(head_at + 1, end)
                   if (_year(content[i]) is None) or _year(content[i]) < year), end)
    return content[:at] + [block] + content[at:]


def add_block(conn: sqlite3.Connection, source: CvSource, set_id: str, data: object) -> SetRecord:
    """Add a referent's block, with its text as the referent has it now, in its section and year order."""
    store.require_editable(conn, KIND, set_id)
    if not isinstance(data, dict):
        raise ValueError("Body must be an object.")
    unknown = set(data) - {"referent_id", "block_id"}
    if unknown:
        raise ValueError(f"Unknown fields: {', '.join(sorted(unknown))}.")
    referent_id = data.get("referent_id")
    if not isinstance(referent_id, str) or not referent_id:
        raise ValueError("referent_id is required.")
    block_id = _block_id(data.get("block_id"))
    ref = referent_blocks(conn, source, referent_id)
    block = next((b for b in ref if b["id"] == block_id), None)
    if block is None:
        raise NotFound(f"No such block in {referent_id}: {block_id}")
    block = {"id": block["id"], "kind": block["kind"], "text": block["text"]}

    def change(content):
        if block_id in [b["id"] for b in content]:
            raise Conflict("This block is already in the set.")
        return _place(content, block, ref)

    return store.mutate(conn, KIND, set_id, change)


def _referent_id(data: object) -> str:
    if not isinstance(data, dict):
        raise ValueError("Body must be an object.")
    unknown = set(data) - {"referent_id"}
    if unknown:
        raise ValueError(f"Unknown fields: {', '.join(sorted(unknown))}.")
    referent_id = data.get("referent_id")
    if not isinstance(referent_id, str) or not referent_id:
        raise ValueError("referent_id is required.")
    return referent_id


def add_all(conn: sqlite3.Connection, source: CvSource, set_id: str, data: object) -> SetRecord:
    """Add every block of a referent that is not in the set yet (Add All on the CV page, dsgnrfhmn
    2026-10-25), each where add_block would put it. One change, so one undo step."""
    store.require_editable(conn, KIND, set_id)
    ref = referent_blocks(conn, source, _referent_id(data))

    def change(content):
        have = {b["id"] for b in content}
        for b in ref:
            if b["id"] not in have:
                content = _place(content, {"id": b["id"], "kind": b["kind"], "text": b["text"]}, ref)
                have.add(b["id"])
        return content

    return store.mutate(conn, KIND, set_id, change)


def remove_all(conn: sqlite3.Connection, set_id: str) -> SetRecord:
    """Empty the set (Remove All on the CV page, dsgnrfhmn 2026-10-25). One undo step."""
    return store.mutate(conn, KIND, set_id, lambda content: [])


def edit_block(conn: sqlite3.Connection, set_id: str, block_id: str, text: object) -> SetRecord:
    cleaned = _text(text)

    def change(content):
        if block_id not in [b["id"] for b in content]:
            raise NotFound(f"No such block: {block_id}")
        return [{**b, "text": cleaned} if b["id"] == block_id else b for b in content]

    return store.mutate(conn, KIND, set_id, change)


def delete_block(conn: sqlite3.Connection, set_id: str, block_id: str) -> SetRecord:
    def change(content):
        if block_id not in [b["id"] for b in content]:
            raise NotFound(f"No such block: {block_id}")
        at = next(i for i, b in enumerate(content) if b["id"] == block_id)
        out = content[:at] + content[at + 1:]
        # The last entry of a section goes with its heading: a heading with nothing under it
        # says nothing. Only the heading directly above, and only when the section is now empty.
        if content[at]["kind"] == "text" and at > 0 and content[at - 1]["kind"] == "heading" \
                and (at >= len(out) or out[at]["kind"] == "heading"):
            out = out[:at - 1] + out[at:]
        return out

    return store.mutate(conn, KIND, set_id, change)


def _slots(content: list[dict[str, str]]) -> list[tuple]:
    """The kind of place each block holds: a heading holds its own (it never moves); any
    other block holds (its section, its year), the year being None for an entry with no
    year. A block may only swap places with blocks that hold the same kind of place, so
    the sections and their year sequence (set by add_block, newest first) never change."""
    out: list[tuple] = []
    section = -1
    for i, b in enumerate(content):
        if b["kind"] == "heading":
            section = i
            out.append(("heading", b["id"]))
        else:
            out.append(("entry", section, _year(b)))
    return out


def reorder(conn: sqlite3.Connection, set_id: str, block_ids: object) -> SetRecord:
    """Sorts the blocks of the set, but only inside one year of one section: the order of
    the years themselves is kept as add_block made it."""
    def change(content):
        if not isinstance(block_ids, list) or sorted(map(str, block_ids)) != sorted(b["id"] for b in content):
            raise ValueError("block_ids must list every block of the set exactly once.")
        by_id = {b["id"]: b for b in content}
        slots = _slots(content)
        slot_of = {b["id"]: slots[i] for i, b in enumerate(content)}
        if [slot_of[i] for i in block_ids] != slots:
            raise ValueError("Blocks can only be sorted within one year of a section.")
        return [by_id[i] for i in block_ids]

    return store.mutate(conn, KIND, set_id, change)


def _pages(record: SetRecord) -> list[list[str]]:
    return mock_paginate.paginate([b["id"] for b in record.content], mock_paginate.CV_PER_PAGE)


def describe(record: SetRecord) -> dict[str, Any]:
    return {
        **record.summary_api(),
        "entries": record.content,
        "pages": len(_pages(record)),
        "pages_origin": mock_paginate.ORIGIN,
    }


def preview(record: SetRecord) -> dict[str, Any]:
    by_id = {b["id"]: b for b in record.content}
    pages = _pages(record)
    return {"pages": [{"entries": [by_id[i] for i in p]} for p in pages],
            "page_count": len(pages), "pages_origin": mock_paginate.ORIGIN}


def ai_catalog(record: SetRecord, source: CvSource) -> edit_cv.Catalog:
    """What the model is shown: the set's blocks and the master (Full) CV."""
    return edit_cv.Catalog(current=tuple(record.content), master=tuple(b.to_api() for b in source.get_blocks("full")))


def apply_ai_edit(conn: sqlite3.Connection, set_id: str, seen: list[dict[str, Any]], new: list[dict[str, Any]]) -> SetRecord:
    """Store the model's new content as one undo step. `seen` is the content the model was
    shown: if the set changed meanwhile, the edit is refused instead of overwriting it."""
    store.require_editable(conn, KIND, set_id)

    def change(content):
        if content != seen:
            raise Conflict("The set changed while the edit was being made. Nothing was applied; try again.")
        return new

    return store.mutate(conn, KIND, set_id, change)
