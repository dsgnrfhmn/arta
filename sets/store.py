"""What every set does, whatever its kind: create, list, save, archive,
delete, change content with undo, undo, redo, reset.

Every function runs inside a caller-owned Database.tx(). Content changes go
through mutate(), which is the only place an undo step is recorded.
"""

from __future__ import annotations

import json
import re
import secrets
import sqlite3
from typing import Any, Callable

from core import sanitize
from core.errors import Conflict, NotFound
from models.set import KINDS, NAME_MAX, NAME_PREFIX, SetRecord
from sets import undo

_SELECT = """
    SELECT s.*,
           EXISTS (SELECT 1 FROM set_undo u WHERE u.set_id = s.id) AS can_undo,
           EXISTS (SELECT 1 FROM set_redo d WHERE d.set_id = s.id) AS can_redo,
           COALESCE(o.application_status IS NOT NULL, 0) AS opportunity_applied
    FROM sets s LEFT JOIN opportunities o ON o.id = s.opportunity_id
"""


def _check_kind(kind: str) -> None:
    if kind not in KINDS:
        raise NotFound(f"No such kind of set: {kind}")


def clean_name(raw: object) -> str:
    name = sanitize.plain_text(raw, NAME_MAX)
    if not name:
        raise ValueError("A name is required.")
    return name


def get(conn: sqlite3.Connection, kind: str, set_id: str) -> SetRecord:
    _check_kind(kind)
    row = conn.execute(_SELECT + " WHERE s.id = ? AND s.kind = ?", (set_id, kind)).fetchone()
    if row is None:
        raise NotFound(f"No such set: {set_id}")
    return SetRecord.from_row(row)


def next_name(conn: sqlite3.Connection, kind: str) -> str:
    """'Portfolio Set N', N one higher than the highest existing number, archived
    sets included, so a generated name never collides (spec 5.3)."""
    prefix = NAME_PREFIX[kind]
    pattern = re.compile(rf"^{re.escape(prefix)} Set (\d+)$")
    numbers = [int(m.group(1)) for (n,) in conn.execute("SELECT name FROM sets WHERE kind = ?", (kind,)) if (m := pattern.match(n))]
    return f"{prefix} Set {max(numbers, default=0) + 1}"


# Words for generated portfolio set names ("Quiet portfolio set"). Plain, calm,
# not about the work, so a name never reads as a verdict on it.
ADJECTIVES = (
    "Amber", "Bright", "Calm", "Clear", "Cosy", "Crisp", "Dusty", "Early", "Gentle", "Golden",
    "Hidden", "Honest", "Hushed", "Ivory", "Jolly", "Keen", "Kind", "Light", "Linen", "Little",
    "Mellow", "Misty", "Modest", "Narrow", "Neat", "Open", "Pale", "Patient", "Quiet", "Rustic",
    "Sandy", "Silver", "Slow", "Soft", "Steady", "Still", "Sunny", "Tidy", "Warm", "Wide",
)


def random_name(conn: sqlite3.Connection, kind: str) -> str:
    """'<Adjective> portfolio set', not used by any set of the kind (archived
    included). When every adjective is taken a number is appended, so the name
    is still unique."""
    label = f"{NAME_PREFIX[kind].lower()} set"
    taken = {n.lower() for (n,) in conn.execute("SELECT name FROM sets WHERE kind = ?", (kind,))}
    free = [a for a in ADJECTIVES if f"{a} {label}".lower() not in taken]
    if free:
        return f"{secrets.choice(free)} {label}"
    n = 2
    while (name := f"{secrets.choice(ADJECTIVES)} {label} {n}").lower() in taken:
        n += 1
    return name


def _default_name(conn: sqlite3.Connection, kind: str) -> str:
    """Portfolio sets get a random adjective name; CV sets keep 'CV Set N'."""
    return random_name(conn, kind) if kind == "portfolio" else next_name(conn, kind)


def create(conn: sqlite3.Connection, kind: str, now: str, name: str | None = None,
           opportunity_id: str | None = None, content: list[Any] | None = None) -> SetRecord:
    _check_kind(kind)
    set_id = secrets.token_hex(8)
    conn.execute(
        "INSERT INTO sets (id, kind, name, opportunity_id, content, created_at, position) VALUES (?,?,?,?,?,?,?)",
        (set_id, kind, clean_name(name) if name is not None else _default_name(conn, kind),
         opportunity_id, json.dumps(content or []), now, _top_position(conn, kind)),
    )
    return get(conn, kind, set_id)


def _top_position(conn: sqlite3.Connection, kind: str) -> int:
    """One above the first set of the kind: a new set opens the sidebar."""
    (lowest,) = conn.execute("SELECT MIN(position) FROM sets WHERE kind = ?", (kind,)).fetchone()
    return (lowest if lowest is not None else 0) - 1


def _legacy_group(r: SetRecord) -> int:
    """The order sets had before the user could place them: 0 unsaved working
    sets, 1 saved, 2 sets whose opportunity is Applied, 3 archived. Used once, to
    give sets made without a position (fixtures) their first place."""
    if r.archived:
        return 3
    if r.opportunity_applied:
        return 2
    return 1 if r.saved else 0


def list_sets(conn: sqlite3.Connection, kind: str) -> list[SetRecord]:
    """The sidebar order (spec 5.3): the user's own, by position. Saving and
    archiving do not move a set; a new set takes the first place."""
    _check_kind(kind)
    rows = conn.execute(_SELECT + " WHERE s.kind = ? ORDER BY s.position, s.created_at DESC, s.id", (kind,))
    return [SetRecord.from_row(r) for r in rows]


def settle_positions(conn: sqlite3.Connection, kind: str) -> None:
    """Gives every set of the kind a place, in the legacy grouping with the newest
    first inside each group. For rows inserted without a position (fixtures); the
    migration does the same in SQL for a database that already holds sets."""
    records = [SetRecord.from_row(r) for r in conn.execute(_SELECT + " WHERE s.kind = ?", (kind,))]

    def recency(r: SetRecord) -> str:
        return r.archived_at or r.saved_at or r.created_at

    records.sort(key=lambda r: r.id)
    records.sort(key=recency, reverse=True)
    records.sort(key=_legacy_group)
    for position, r in enumerate(records):
        conn.execute("UPDATE sets SET position = ? WHERE id = ?", (position, r.id))


def reorder_sidebar(conn: sqlite3.Connection, kind: str, ids: object) -> None:
    """Sets the sidebar order of the kind. `ids` must name exactly the sets of
    the kind that were shown (archived ones are not shown, and keep their place
    after them), each once. A stale or partial list is refused, not guessed."""
    _check_kind(kind)
    if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids) or len(set(ids)) != len(ids):
        raise ValueError("ids must be a list of distinct set ids.")
    rows = conn.execute("SELECT id, archived FROM sets WHERE kind = ?", (kind,)).fetchall()
    shown = {r["id"] for r in rows if not r["archived"]}
    if set(ids) != shown:
        raise ValueError("ids must name every set of the sidebar, each once.")
    for position, set_id in enumerate(ids):
        conn.execute("UPDATE sets SET position = ? WHERE id = ?", (position, set_id))
    # archived sets are not in the list; they follow it, in their existing order
    rest = [r.id for r in list_sets(conn, kind) if r.id not in shown]
    for offset, set_id in enumerate(rest):
        conn.execute("UPDATE sets SET position = ? WHERE id = ?", (len(ids) + offset, set_id))


def _editable(r: SetRecord) -> None:
    if r.archived:
        raise Conflict("An archived set cannot be changed.")


def require_editable(conn: sqlite3.Connection, kind: str, set_id: str) -> None:
    """404 for a missing set, 409 for an archived one. Callers that validate a
    body first call this, so the state of the set is reported before the body."""
    _editable(get(conn, kind, set_id))


def save(conn: sqlite3.Connection, kind: str, set_id: str, name: object, now: str) -> SetRecord:
    """Name it and keep it: the current content becomes what Reset returns to."""
    r = get(conn, kind, set_id)
    _editable(r)
    conn.execute(
        "UPDATE sets SET name = ?, saved = 1, saved_at = ?, baseline = content WHERE id = ?",
        (clean_name(name), now, set_id),
    )
    return get(conn, kind, set_id)


def rename(conn: sqlite3.Connection, kind: str, set_id: str, name: object) -> SetRecord:
    """Only the name. The saved flag, the content and the undo steps stay as they
    are, so a rename is neither a save nor an undo step."""
    r = get(conn, kind, set_id)
    _editable(r)
    conn.execute("UPDATE sets SET name = ? WHERE id = ?", (clean_name(name), set_id))
    return get(conn, kind, set_id)


def duplicate(conn: sqlite3.Connection, kind: str, set_id: str, name: object, now: str) -> SetRecord:
    """A named copy of the set's content: saved (it was named on purpose), with no
    undo steps, not tied to the original's opportunity, never archived. The
    original is not touched. The source may itself be archived."""
    r = get(conn, kind, set_id)
    copy_name = clean_name(name)
    new_id = secrets.token_hex(8)
    # A portfolio set's chosen sheet layouts belong to that set only (dsgnrfhmn, 2026-10-24):
    # the copy starts from the automatic layout.
    content = [{k: v for k, v in e.items() if k not in ("layout_plan", "exhibition_plan")} for e in r.content] if kind == "portfolio" else r.content
    conn.execute(
        "INSERT INTO sets (id, kind, name, saved, saved_at, baseline, content, created_at, position)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (new_id, kind, copy_name, 1, now, json.dumps(content), json.dumps(content), now,
         _top_position(conn, kind)),
    )
    return get(conn, kind, new_id)


def set_saved(conn: sqlite3.Connection, kind: str, set_id: str, saved: object, now: str) -> SetRecord:
    """The sidebar's save icon: toggles the flag without touching the name."""
    if not isinstance(saved, bool):
        raise ValueError("saved must be true or false.")
    r = get(conn, kind, set_id)
    _editable(r)
    if saved:
        conn.execute("UPDATE sets SET saved = 1, saved_at = ?, baseline = content WHERE id = ?", (now, set_id))
    else:
        conn.execute("UPDATE sets SET saved = 0, saved_at = NULL WHERE id = ?", (set_id,))
    return get(conn, kind, set_id)


def archive(conn: sqlite3.Connection, kind: str, set_id: str, now: str) -> SetRecord:
    r = get(conn, kind, set_id)
    if r.archived:
        raise Conflict("The set is already archived.")
    conn.execute("UPDATE sets SET archived = 1, archived_at = ? WHERE id = ?", (now, set_id))
    return get(conn, kind, set_id)


def unarchive(conn: sqlite3.Connection, kind: str, set_id: str) -> SetRecord:
    """A mistaken archive is undone here. Content, name, saved state and undo
    steps are as they were, so the set goes back to its own sidebar group."""
    r = get(conn, kind, set_id)
    if not r.archived:
        raise Conflict("The set is not archived.")
    conn.execute("UPDATE sets SET archived = 0, archived_at = NULL WHERE id = ?", (set_id,))
    return get(conn, kind, set_id)


def delete(conn: sqlite3.Connection, kind: str, set_id: str) -> None:
    get(conn, kind, set_id)
    conn.execute("DELETE FROM sets WHERE id = ?", (set_id,))    # undo steps cascade


def mutate(conn: sqlite3.Connection, kind: str, set_id: str, change: Callable[[list[Any]], list[Any]]) -> SetRecord:
    """Apply an undoable change to the content. A change that leaves the content
    as it was records no step."""
    r = get(conn, kind, set_id)
    _editable(r)
    before = r.content
    after = change(json.loads(json.dumps(before)))       # the callback works on a copy
    if after != before:
        undo.push(conn, set_id, before)
        undo.clear_redo(conn, set_id)                    # a new change ends the redo line
        conn.execute("UPDATE sets SET content = ? WHERE id = ?", (json.dumps(after), set_id))
    return get(conn, kind, set_id)


def undo_last(conn: sqlite3.Connection, kind: str, set_id: str) -> SetRecord:
    r = get(conn, kind, set_id)
    _editable(r)
    snapshot = undo.pop(conn, set_id)
    if snapshot is None:
        raise Conflict("Nothing to undo.")
    undo.push_redo(conn, set_id, r.content)
    conn.execute("UPDATE sets SET content = ? WHERE id = ?", (json.dumps(snapshot), set_id))
    return get(conn, kind, set_id)


def redo_last(conn: sqlite3.Connection, kind: str, set_id: str) -> SetRecord:
    r = get(conn, kind, set_id)
    _editable(r)
    snapshot = undo.pop_redo(conn, set_id)
    if snapshot is None:
        raise Conflict("Nothing to redo.")
    undo.push(conn, set_id, r.content)
    conn.execute("UPDATE sets SET content = ? WHERE id = ?", (json.dumps(snapshot), set_id))
    return get(conn, kind, set_id)


def reset(conn: sqlite3.Connection, kind: str, set_id: str) -> SetRecord:
    """Back to the last saved content (empty if never saved). Not undoable: the
    undo and redo steps are cleared."""
    r = get(conn, kind, set_id)
    _editable(r)
    conn.execute("UPDATE sets SET content = baseline WHERE id = ?", (set_id,))
    undo.clear(conn, set_id)
    return get(conn, kind, set_id)
