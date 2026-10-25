"""What a portfolio set's content means: a list of project entries.

    {"id", "project_id", "works": [image_id...], "exhibitions": [image_id...],
     "text_amount": "short" | "middle" | "full",
     "text_override": str}                      # optional, set by Edit with AI

and at most one cover element, the set's own edits of the cover's texts (dsgnrfhmn,
2026-10-24), which is not a project entry:

    {"id": "cover", "kind": "cover", "texts": {field: text or [lines]}}

It lives in the content so Undo, Redo, Reset, Save and Save as new treat it like
every other edit; everything that reads the projects goes through entries().

`works` are the project's photos of the work and `exhibitions` its exhibition
photos, told apart by `ImageRef.role` (the two picker rows of the Add project
popup). Each list
is in pick order, and that order is the order in the set.

Every change goes through sets.store.mutate, so each one is an undo step.
"""

from __future__ import annotations

import re
import secrets
import sqlite3
from typing import Any

from core.errors import Conflict, NotFound
from layout import mock_paginate, sheets
from models.project import Project
from models.set import SetRecord
from pipeline import edit_portfolio
from sets import store
from sources.project_source import NotFound as ProjectNotFound
from sources.project_source import ProjectSource

KIND = "portfolio"
TEXT_AMOUNTS = ("short", "middle", "full")
COVER_ID = "cover"


def _is_cover(element: dict[str, Any]) -> bool:
    return element.get("kind") == "cover"


def entries(content: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The project entries of a set's content, without its cover element."""
    return [e for e in content if not _is_cover(e)]


def cover_texts(content: list[dict[str, Any]]) -> dict[str, Any]:
    """The set's own cover texts by field ({} when it has none)."""
    return next((dict(e.get("texts") or {}) for e in content if _is_cover(e)), {})


def _entry_ids(content: list[dict[str, Any]]) -> list[str]:
    return [e["id"] for e in entries(content)]


def _pick_list(project: Project, raw: object, name: str, want_exhibition: bool) -> list[str]:
    if not isinstance(raw, list) or not all(isinstance(i, str) for i in raw):
        raise ValueError(f"{name} must be a list of image ids.")
    if len(set(raw)) != len(raw):
        raise ValueError(f"{name} lists an image twice.")
    by_id = {i.id: i for i in project.images}
    for image_id in raw:
        image = by_id.get(image_id)
        if image is None:
            raise ValueError(f"{name}: image {image_id} is not in this project.")
        if (image.role == "exhibition") != want_exhibition:
            raise ValueError(f"{name}: image {image_id} belongs in {'works' if want_exhibition else 'exhibitions'}.")
    return list(raw)


def entry_from(source: ProjectSource, data: object, entry_id: str) -> dict[str, Any]:
    """Validate a client entry against the real project and build the stored one."""
    if not isinstance(data, dict):
        raise ValueError("Body must be an object.")
    unknown = set(data) - {"project_id", "works", "exhibitions", "text_amount"}
    if unknown:
        raise ValueError(f"Unknown fields: {', '.join(sorted(unknown))}.")
    project_id = data.get("project_id")
    if not isinstance(project_id, str) or not project_id:
        raise ValueError("project_id is required.")
    amount = data.get("text_amount", "short")
    if amount not in TEXT_AMOUNTS:
        raise ValueError(f"text_amount must be one of {', '.join(TEXT_AMOUNTS)}.")
    try:
        project = source.get_project(project_id)
    except ProjectNotFound as e:
        raise NotFound(str(e)) from None
    return {
        "id": entry_id,
        "project_id": project_id,
        "works": _pick_list(project, data.get("works", []), "works", False),
        "exhibitions": _pick_list(project, data.get("exhibitions", []), "exhibitions", True),
        "text_amount": amount,
    }


def full_content(projects: list[Project]) -> list[dict[str, Any]]:
    """Every project with all its pictures and its full text, in the source's order: the
    content of a New Full Set, which the artist then thins out (dsgnrfhmn, 2026-10-24)."""
    return [{
        "id": secrets.token_hex(8),
        "project_id": p.id,
        "works": [i.id for i in p.images if i.role != "exhibition"],
        "exhibitions": [i.id for i in p.images if i.role == "exhibition"],
        "text_amount": "full",
    } for p in projects]


def create_full(conn: sqlite3.Connection, projects: list[Project], now: str) -> SetRecord:
    """A new set with a generated name, holding full_content(projects) from the start."""
    return store.create(conn, KIND, now, content=full_content(projects))


def add_entry(conn: sqlite3.Connection, source: ProjectSource, set_id: str, data: object) -> SetRecord:
    store.require_editable(conn, KIND, set_id)
    entry = entry_from(source, data, secrets.token_hex(8))
    return store.mutate(conn, KIND, set_id, lambda content: content + [entry])


def edit_entry(conn: sqlite3.Connection, source: ProjectSource, set_id: str, entry_id: str, data: object) -> SetRecord:
    store.require_editable(conn, KIND, set_id)
    entry = entry_from(source, data, entry_id)

    def change(content):
        if entry_id not in _entry_ids(content):
            raise NotFound(f"No such entry: {entry_id}")
        # The project popup knows nothing of the AI's text or the chosen layouts: an
        # edit there keeps both.
        kept = ("text_override", "layout_plan", "exhibition_plan", "title_override", "subtitle_override", "caption_overrides")
        return [{**entry, **{k: e[k] for k in kept if k in e}} if e["id"] == entry_id else e for e in content]

    return store.mutate(conn, KIND, set_id, change)


def set_layout(conn: sqlite3.Connection, set_id: str, entry_id: str, plan: object, part: object = "works") -> SetRecord:
    """The set's own choice of sheet layouts for one entry (dsgnrfhmn, 2026-10-24). part
    "works": the opening's layout, then one per works sheet; part "exhibitions": one per
    sheet of exhibition views (layout/sheets.py). An empty list goes back to the
    automatic layout. One undo step. Kept in this set only: a copy of the set does not
    carry it (store.duplicate)."""
    store.require_editable(conn, KIND, set_id)
    if part not in ("works", "exhibitions"):
        raise ValueError("part must be works or exhibitions.")
    if not isinstance(plan, list) or len(plan) > 60 or not all(isinstance(p, str) for p in plan):
        raise ValueError("plan must be a list of layout names.")
    later = plan[1:] if part == "works" else plan
    if part == "works" and plan and plan[0] not in sheets.OPENING_LAYOUTS:
        raise ValueError(f"The first sheet's layout must be one of {', '.join(sheets.OPENING_LAYOUTS)}.")
    if any(p not in sheets.WORK_LAYOUTS for p in later):
        raise ValueError(f"A layout must be one of {', '.join(sheets.WORK_LAYOUTS)}.")
    key = "layout_plan" if part == "works" else "exhibition_plan"

    def change(content):
        if entry_id not in _entry_ids(content):
            raise NotFound(f"No such entry: {entry_id}")
        out = []
        for e in content:
            if e["id"] == entry_id:
                e = {k: v for k, v in e.items() if k != key}
                if plan:
                    e[key] = list(plan)
            out.append(e)
        return out

    return store.mutate(conn, KIND, set_id, change)


TEXT_FIELDS = {"title": ("title_override", 300), "subtitle": ("subtitle_override", 100), "text": ("text_override", 20_000)}


def set_texts(conn: sqlite3.Connection, set_id: str, entry_id: str, data: object) -> SetRecord:
    """The set's own edits of an entry's texts on the sheets (dsgnrfhmn, 2026-10-24): title,
    subtitle (the year line), the project text, and captions by image id. A string
    replaces that text in this set only; an empty string or null goes back to the
    project's own. Drive and every other set are untouched. One undo step."""
    store.require_editable(conn, KIND, set_id)
    if not isinstance(data, dict) or not data:
        raise ValueError("Body must be an object with title, subtitle, text or captions.")
    unknown = set(data) - {*TEXT_FIELDS, "captions"}
    if unknown:
        raise ValueError(f"Unknown fields: {', '.join(sorted(unknown))}.")

    def clean(value: object, limit: int, name: str) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError(f"{name} must be text.")
        value = value.replace("\r\n", "\n").strip()
        if len(value) > limit:
            raise ValueError(f"{name} is longer than {limit} characters.")
        return value or None

    def change(content):
        entry = next((e for e in entries(content) if e["id"] == entry_id), None)
        if entry is None:
            raise NotFound(f"No such entry: {entry_id}")
        e = dict(entry)
        for field, (key, limit) in TEXT_FIELDS.items():
            if field in data:
                value = clean(data[field], limit, field)
                e.pop(key, None)
                if value:
                    e[key] = value
        if "captions" in data:
            captions = data["captions"]
            if not isinstance(captions, dict):
                raise ValueError("captions must map image ids to text.")
            known = set(e["works"]) | set(e["exhibitions"])
            merged = dict(e.get("caption_overrides") or {})
            for image_id, value in captions.items():
                if image_id not in known:
                    raise ValueError(f"Image {image_id} is not in this entry.")
                value = clean(value, 1000, "caption")
                merged.pop(image_id, None)
                if value:
                    merged[image_id] = value
            e.pop("caption_overrides", None)
            if merged:
                e["caption_overrides"] = merged
        return [e if x["id"] == entry_id else x for x in content]

    return store.mutate(conn, KIND, set_id, change)


def delete_entry(conn: sqlite3.Connection, set_id: str, entry_id: str) -> SetRecord:
    def change(content):
        if entry_id not in _entry_ids(content):
            raise NotFound(f"No such entry: {entry_id}")
        return [e for e in content if e["id"] != entry_id]

    return store.mutate(conn, KIND, set_id, change)


def delete_entries(conn: sqlite3.Connection, set_id: str, entry_ids: object) -> SetRecord:
    """Several projects out of the set at once (the table's bulk Delete, dsgnrfhmn 2026-10-25):
    one undo step, so one Undo brings them all back. Every id must be in the set."""
    if not isinstance(entry_ids, list) or not entry_ids or not all(isinstance(i, str) for i in entry_ids):
        raise ValueError("entry_ids must be a non-empty list of entry ids.")
    wanted = set(entry_ids)

    def change(content):
        missing = wanted - set(_entry_ids(content))
        if missing:
            raise NotFound(f"No such entry: {sorted(missing)[0]}")
        return [e for e in content if e.get("id") not in wanted or _is_cover(e)]

    return store.mutate(conn, KIND, set_id, change)


def reorder(conn: sqlite3.Connection, set_id: str, entry_ids: object) -> SetRecord:
    def change(content):
        if not isinstance(entry_ids, list) or sorted(map(str, entry_ids)) != sorted(_entry_ids(content)):
            raise ValueError("entry_ids must list every entry of the set exactly once.")
        by_id = {e["id"]: e for e in entries(content)}
        return [by_id[i] for i in entry_ids] + [e for e in content if _is_cover(e)]

    return store.mutate(conn, KIND, set_id, change)


def _projects_of(record: SetRecord, source: ProjectSource | None) -> dict[str, Project | None]:
    """Each project of the set, read once; None when it cannot be found."""
    projects: dict[str, Project | None] = {}
    for e in entries(record.content):
        pid = e["project_id"]
        if pid not in projects:
            try:
                projects[pid] = source.get_project(pid) if source else None
            except ProjectNotFound:
                projects[pid] = None
    return projects


def _sheets(record: SetRecord, projects: dict[str, Project | None]) -> list[dict[str, Any]]:
    return [s for e in entries(record.content) for s in sheets.entry_sheets(e, projects[e["project_id"]])]


def describe(record: SetRecord, source: ProjectSource | None, has_cover: bool = False) -> dict[str, Any]:
    """The set as the page needs it: entries with their project's name, and the
    page count (pages_origin says it is an estimate; the cover counts as one)."""
    projects = _projects_of(record, source)
    shown = []
    for e in entries(record.content):
        project = projects[e["project_id"]]
        shown.append({**e, "project_name": project.name if project else None, "project_missing": project is None,
                      "image_count": len(e["works"]) + len(e["exhibitions"])})
    return {
        **record.summary_api(),
        "entries": shown,
        "pages": len(_sheets(record, projects)) + (1 if has_cover and shown else 0),
        "pages_origin": mock_paginate.ORIGIN,
    }


def preview(record: SetRecord, source: ProjectSource | None, cover: dict[str, Any] | None = None) -> dict[str, Any]:
    """One sheet per page, in set order (layout/sheets.py), the cover sheet first when
    there is one and the set is not empty, with the set's own cover texts over the
    cover's."""
    pages = _sheets(record, _projects_of(record, source))
    if cover is not None and pages:
        own = cover_texts(record.content)
        if own and cover.get("texts"):
            cover = {**cover, "texts": {**cover["texts"], **own}}
        pages = [cover, *pages]
    return {"pages": pages, "page_count": len(pages), "pages_origin": mock_paginate.ORIGIN}


def ai_catalog(record: SetRecord, source: ProjectSource) -> edit_portfolio.Catalog:
    """What the model is shown: every project that exists, and the set's entries."""
    return edit_portfolio.Catalog(projects=tuple(source.list_projects()), entries=tuple(entries(record.content)))


def apply_ai_edit(conn: sqlite3.Connection, set_id: str, seen: list[dict[str, Any]], new: list[dict[str, Any]]) -> SetRecord:
    """Store the model's new entries as one undo step, keeping the set's cover texts. `seen`
    is the content when the model was shown it: if the set changed meanwhile, the edit is
    refused instead of overwriting it."""
    store.require_editable(conn, KIND, set_id)

    def change(content):
        if content != seen:
            raise Conflict("The set changed while the edit was being made. Nothing was applied; try again.")
        return entries(new) + [e for e in content if _is_cover(e)]

    return store.mutate(conn, KIND, set_id, change)


# The cover's texts the set may change (dsgnrfhmn, 2026-10-24), as the cover text file names
# them. The list fields arrive as one text: details and contacts one line each,
# paragraphs split at a blank line.
COVER_FIELDS = {"title": 100, "name": 200, "details": 1000, "heading": 100, "paragraphs": 20_000, "contacts": 1000}
COVER_LISTS = {"details": r"\n+", "contacts": r"\n+", "paragraphs": r"\n\s*\n"}


def set_cover_texts(conn: sqlite3.Connection, set_id: str, data: object) -> SetRecord:
    """The set's own edits of the cover's texts. A text replaces that field in this set
    only; an empty text or null goes back to the cover's own. The cover file and every
    other set are untouched. One undo step."""
    store.require_editable(conn, KIND, set_id)
    if not isinstance(data, dict) or not data:
        raise ValueError(f"Body must be an object with {', '.join(COVER_FIELDS)}.")
    unknown = set(data) - set(COVER_FIELDS)
    if unknown:
        raise ValueError(f"Unknown fields: {', '.join(sorted(unknown))}.")
    changes: dict[str, Any] = {}
    for field, value in data.items():
        if value is not None and not isinstance(value, str):
            raise ValueError(f"{field} must be text.")
        value = (value or "").replace("\r\n", "\n").strip()
        if len(value) > COVER_FIELDS[field]:
            raise ValueError(f"{field} is longer than {COVER_FIELDS[field]} characters.")
        if field in COVER_LISTS:
            parts = [" ".join(p.split()) if field == "paragraphs" else p.strip() for p in re.split(COVER_LISTS[field], value)]
            changes[field] = [p for p in parts if p] or None
        else:
            changes[field] = value or None

    def change(content):
        texts = cover_texts(content)
        for field, value in changes.items():
            texts.pop(field, None)
            if value:
                texts[field] = value
        rest = [e for e in content if not _is_cover(e)]
        return rest + ([{"id": COVER_ID, "kind": "cover", "texts": texts}] if texts else [])

    return store.mutate(conn, KIND, set_id, change)


def delete_sheet(conn: sqlite3.Connection, source: ProjectSource, set_id: str, entry_id: str, part: object,
                 index: object) -> SetRecord:
    """Delete one page of an entry from this set (dsgnrfhmn, 2026-10-24): the pictures on it
    leave the entry (with their edited captions), and the opening takes its text with it
    (the paragraphs it shows; the text sheets after it keep theirs). The other pages of
    the entry keep the layouts they have now. An entry left with nothing to show leaves
    the set. part "works" addresses a page by plan_index, "exhibitions" by
    exhibition_index (layout/sheets.py). One undo step; Drive is untouched."""
    store.require_editable(conn, KIND, set_id)
    if part not in ("works", "exhibitions"):
        raise ValueError("part must be works or exhibitions.")
    if not isinstance(index, int) or isinstance(index, bool) or index < 0:
        raise ValueError("index must be a page number from 0.")
    key = "plan_index" if part == "works" else "exhibition_index"

    def change(content):
        entry = next((e for e in entries(content) if e["id"] == entry_id), None)
        if entry is None:
            raise NotFound(f"No such entry: {entry_id}")
        try:
            project = source.get_project(entry["project_id"])
        except ProjectNotFound as e:
            raise NotFound(str(e)) from None
        drawn = [s for s in sheets.entry_sheets(entry, project) if s.get(key) is not None]
        page = next((s for s in drawn if s[key] == index), None)
        if page is None:
            raise NotFound(f"No such page: {part} {index}")
        gone = {i["id"] for i in page["images"]}
        e = dict(entry)
        e["works"] = [i for i in e["works"] if i not in gone]
        e["exhibitions"] = [i for i in e["exhibitions"] if i not in gone]
        captions = {k: v for k, v in (e.get("caption_overrides") or {}).items() if k not in gone}
        e.pop("caption_overrides", None)
        if captions:
            e["caption_overrides"] = captions
        if part == "works" and index == 0:
            if page["paragraphs"]:
                groups = sheets.paragraphs(page["entry_text"], sheets.TEXT_ON_OPENING)
                e["text_override"] = "\n\n".join(p for g in groups[1:] for p in g)
            elif not page["entry_text"].strip():
                e["text_override"] = ""            # an empty opening: mark its text deleted too
        # The pages left keep their layouts: the plan becomes their layouts as drawn now.
        # A page that becomes the opening takes the opening layout nearest its own: a grid
        # the grid beside the text, one picture the picture beside the text; a row of two
        # or three has none, and the opening is chosen automatically ("auto").
        layouts = [s["layout"] for s in drawn if s[key] != index]
        plan_key = "layout_plan" if part == "works" else "exhibition_plan"
        e.pop(plan_key, None)
        if part == "works" and layouts and layouts[0] not in sheets.OPENING_LAYOUTS:
            layouts[0] = {"grid": "grid-side", "full": "figure-side", "figure": "figure-side"}.get(layouts[0], "auto")
        if layouts:
            e[plan_key] = layouts
        text_left = e["text_override"] if isinstance(e.get("text_override"), str) else sheets.description(project)
        if not e["works"] and not e["exhibitions"] and not text_left.strip():
            return [x for x in content if x.get("id") != entry_id]
        return [e if x.get("id") == entry_id else x for x in content]

    return store.mutate(conn, KIND, set_id, change)
