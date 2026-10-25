"""Undo and redo for sets, shared by Portfolio and CV.

Snapshots: before an undoable change the set's content is stored as it was;
Undo puts the newest snapshot back and drops it, and stores the content it
replaced as a redo step; Redo does the reverse. Both stacks store opaque JSON
and know nothing about projects or blocks. 20 steps per set, in the database,
so they survive a reload and a restart. A new change clears the redo steps
(there is no branching history). Reset clears both stacks.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from models.set import UNDO_LIMIT

_TABLES = ("set_undo", "set_redo")


def _push(conn: sqlite3.Connection, table: str, set_id: str, content: Any) -> None:
    assert table in _TABLES
    conn.execute(f"INSERT INTO {table} (set_id, snapshot) VALUES (?, ?)", (set_id, json.dumps(content)))
    conn.execute(
        f"DELETE FROM {table} WHERE set_id = ? AND id NOT IN "
        f"(SELECT id FROM {table} WHERE set_id = ? ORDER BY id DESC LIMIT ?)",
        (set_id, set_id, UNDO_LIMIT),
    )


def _pop(conn: sqlite3.Connection, table: str, set_id: str) -> Any | None:
    assert table in _TABLES
    row = conn.execute(f"SELECT id, snapshot FROM {table} WHERE set_id = ? ORDER BY id DESC LIMIT 1", (set_id,)).fetchone()
    if row is None:
        return None
    conn.execute(f"DELETE FROM {table} WHERE id = ?", (row["id"],))
    return json.loads(row["snapshot"])


def push(conn: sqlite3.Connection, set_id: str, content: Any) -> None:
    _push(conn, "set_undo", set_id, content)


def pop(conn: sqlite3.Connection, set_id: str) -> Any | None:
    """The newest snapshot, removed. None when there is nothing to undo."""
    return _pop(conn, "set_undo", set_id)


def push_redo(conn: sqlite3.Connection, set_id: str, content: Any) -> None:
    _push(conn, "set_redo", set_id, content)


def pop_redo(conn: sqlite3.Connection, set_id: str) -> Any | None:
    """The newest redo snapshot, removed. None when there is nothing to redo."""
    return _pop(conn, "set_redo", set_id)


def clear_redo(conn: sqlite3.Connection, set_id: str) -> None:
    conn.execute("DELETE FROM set_redo WHERE set_id = ?", (set_id,))


def clear(conn: sqlite3.Connection, set_id: str) -> None:
    conn.execute("DELETE FROM set_undo WHERE set_id = ?", (set_id,))
    clear_redo(conn, set_id)
