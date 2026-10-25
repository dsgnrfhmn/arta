"""Replies matched to applied opportunities (Tracker, spec 7.5 and 7.6).

Bodies come from strangers: they are sanitized to plain text before they are
stored, and the API returns them as text for the page to render with
textContent. Nothing here reads a body as an instruction.

Marking read is local state; the mailbox is never written to.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from core import sanitize
from core.errors import NotFound
from models import opportunity
from sources.mail_updates import IncomingUpdate

SENDER_MAX = 320
SUBJECT_MAX = 500
BODY_MAX = 20_000


def _clean(u: IncomingUpdate) -> tuple[str, str, str, str, str]:
    return (
        sanitize.plain_text(u.sender, SENDER_MAX),
        u.received_at,
        sanitize.plain_text(u.subject, SUBJECT_MAX),
        sanitize.plain_text(u.body, BODY_MAX),
        u.message_id,
    )


def store(conn: sqlite3.Connection, incoming: list[IncomingUpdate]) -> int:
    """Insert what has not been seen. Returns how many messages are new."""
    new = 0
    for u in incoming:
        sender, received_at, subject, body, message_id = _clean(u)
        cur = conn.execute(
            "INSERT OR IGNORE INTO updates (message_id, opportunity_id, sender, received_at, subject, body)"
            " VALUES (?,?,?,?,?,?)",
            (message_id, u.opportunity_id, sender, received_at, subject, body),
        )
        new += cur.rowcount
    return new


def unread_total(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM updates WHERE read = 0").fetchone()[0]


def refresh(conn: sqlite3.Connection, incoming: list[IncomingUpdate]) -> dict[str, int]:
    """Keep what is new from a read of the mailbox. The read itself happens before
    the transaction, so a slow mailbox never holds the database. No model call, so
    no cost popup.

    `matched` is the number of applications with at least one reply after this
    run (archived ones included, spec 7.4); `new` the messages stored by it;
    `unread` every unread message."""
    new = store(conn, incoming)
    matched = conn.execute("SELECT COUNT(DISTINCT opportunity_id) FROM updates").fetchone()[0]
    return {"matched": matched, "new": new, "unread": unread_total(conn)}


def list_for(conn: sqlite3.Connection, opportunity_id: str) -> list[dict[str, Any]]:
    """The replies for one row, newest first (spec 7.6)."""
    if conn.execute("SELECT 1 FROM opportunities WHERE id = ?", (opportunity_id,)).fetchone() is None:
        raise NotFound(f"No such opportunity: {opportunity_id}")
    rows = conn.execute(
        "SELECT id, sender, received_at, subject, body, read FROM updates"
        " WHERE opportunity_id = ? ORDER BY received_at DESC, id DESC",
        (opportunity_id,),
    )
    return [{**dict(r), "read": bool(r["read"])} for r in rows]


def mark_read(conn: sqlite3.Connection, opportunity_id: str) -> None:
    """Opening the popup reads every reply on the row (spec 7.5)."""
    if conn.execute("SELECT 1 FROM opportunities WHERE id = ?", (opportunity_id,)).fetchone() is None:
        raise NotFound(f"No such opportunity: {opportunity_id}")
    conn.execute("UPDATE updates SET read = 1 WHERE opportunity_id = ? AND read = 0", (opportunity_id,))
