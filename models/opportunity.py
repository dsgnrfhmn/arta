"""The single opportunity record.

One record per opportunity with independent fields. Applied-and-still-in-
Analysed is representable because `application` is not a lifecycle stage.
The Tracker is `application is not None`.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from core import sanitize
from core.errors import Conflict, NotFound  # noqa: F401  (re-exported: existing callers use opportunity.NotFound)
from core.db import Database

STAGES = ("filtered", "for_review", "analysed")
TYPES = ("open_call", "grant")
APPLICATION_STATUSES = ("applied", "approved", "declined")
ANALYSIS_ORIGINS = ("mock", "live")


@dataclass(frozen=True)
class Application:
    status: str
    applied_at: str
    # Tracker's own Archived tab; independent of Opportunity.archived (For You).
    archived: bool = False
    archived_at: str | None = None


@dataclass(frozen=True)
class Opportunity:
    id: str
    stage: str
    name: str
    type: str
    source: str
    created_at: str
    link: str | None = None
    description: str = ""
    prepared: str | None = None  # cv_set_id, For You only
    application: Application | None = None
    archived: bool = False
    archived_at: str | None = None
    saved: bool = False
    summary: str | None = None
    eligible: str | None = None
    relevance: float | None = None
    deadline: str | None = None
    residency_start: str | None = None
    residency_end: str | None = None
    decision_date: str | None = None
    notes: str = ""
    analysis_origin: str | None = None  # "mock" | "live"
    filter_reason: str | None = None  # Filtered only
    updates_total: int = 0  # replies matched to this application (Tracker)
    updates_unread: int = 0
    body: str = ""  # intake email text for analysis; internal, not in to_api
    # The analysis blocks (pipeline/opencall_summarise.DETAIL_KEYS), None before analysis.
    details: dict[str, str] | None = None

    def to_api(self) -> dict[str, Any]:
        """JSON shape of the record."""
        return {
            "id": self.id,
            "stage": self.stage,
            "prepared": self.prepared,
            "application": (
                {
                    "status": self.application.status,
                    "applied_at": self.application.applied_at,
                    "archived": self.application.archived,
                    "archived_at": self.application.archived_at,
                }
                if self.application
                else None
            ),
            "flags": {"archived": self.archived, "saved": self.saved},
            "archived_at": self.archived_at,
            "fields": {
                "name": self.name,
                "type": self.type,
                "source": self.source,
                "link": self.link,
                "description": self.description,
                "summary": self.summary,
                "eligible": self.eligible,
                "relevance": self.relevance,
                "deadline": self.deadline,
                "residency_start": self.residency_start,
                "residency_end": self.residency_end,
                "decision_date": self.decision_date,
                "notes": self.notes,
                "analysis_origin": self.analysis_origin,
                "filter_reason": self.filter_reason,
                "details": self.details,
            },
            "updates": {"total": self.updates_total, "unread": self.updates_unread} if self.application else None,
            "created_at": self.created_at,
        }

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Opportunity":
        status = row["application_status"]
        return cls(
            id=row["id"],
            stage=row["stage"],
            name=row["name"],
            type=row["type"],
            source=row["source"],
            created_at=row["created_at"],
            link=row["link"],
            description=row["description"],
            prepared=row["prepared_cv_set_id"],
            application=(
                Application(
                    status,
                    row["applied_at"],
                    bool(row["application_archived"]),
                    row["application_archived_at"],
                )
                if status
                else None
            ),
            archived=bool(row["archived"]),
            archived_at=row["archived_at"],
            saved=bool(row["saved"]),
            summary=row["summary"],
            eligible=row["eligible"],
            relevance=row["relevance"],
            deadline=row["deadline"],
            residency_start=row["residency_start"],
            residency_end=row["residency_end"],
            decision_date=row["decision_date"],
            notes=row["notes"],
            analysis_origin=row["analysis_origin"],
            filter_reason=row["filter_reason"],
            updates_total=row["updates_total"] if "updates_total" in row.keys() else 0,
            updates_unread=row["updates_unread"] if "updates_unread" in row.keys() else 0,
            body=row["body"] if "body" in row.keys() else "",
            details=_details(row["details"]) if "details" in row.keys() else None,
        )

    @classmethod
    def from_seed(cls, data: dict[str, Any]) -> "Opportunity":
        """Build from a fixture entry (flat keys, `application` nested)."""
        data = dict(data)
        app = data.pop("application", None)
        return cls(application=Application(**app) if app else None, **data)


def _details(raw: str | None) -> dict[str, str] | None:
    """The stored JSON object, or None. A value that is not an object is treated as
    absent rather than breaking a list."""
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def insert(conn: sqlite3.Connection, o: Opportunity) -> None:
    """Insert inside a caller-owned transaction. The schema CHECKs are the validation."""
    conn.execute(
        """
        INSERT INTO opportunities (
            id, stage, prepared_cv_set_id, application_status, applied_at,
            archived, archived_at, saved, name, type, source, link, description,
            summary, eligible, relevance, deadline, residency_start, residency_end,
            decision_date, notes, analysis_origin, created_at,
            application_archived, application_archived_at, filter_reason, body, details
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            o.id, o.stage, o.prepared,
            o.application.status if o.application else None,
            o.application.applied_at if o.application else None,
            int(o.archived), o.archived_at, int(o.saved), o.name, o.type, o.source,
            o.link, o.description, o.summary, o.eligible, o.relevance, o.deadline,
            o.residency_start, o.residency_end, o.decision_date, o.notes,
            o.analysis_origin, o.created_at,
            int(o.application.archived) if o.application else 0,
            o.application.archived_at if o.application else None,
            o.filter_reason, o.body, json.dumps(o.details, ensure_ascii=False) if o.details else None,
        ),
    )


# Every read of a record carries its update counts, so a Tracker row and its
# Analysed twin can never disagree about them.
_SELECT = """
    SELECT o.*,
           (SELECT COUNT(*) FROM updates u WHERE u.opportunity_id = o.id) AS updates_total,
           (SELECT COUNT(*) FROM updates u WHERE u.opportunity_id = o.id AND u.read = 0) AS updates_unread
    FROM opportunities o
"""


def get(db: Database, opportunity_id: str) -> Opportunity | None:
    with db.read() as conn:
        row = conn.execute(_SELECT + " WHERE o.id = ?", (opportunity_id,)).fetchone()
    return Opportunity.from_row(row) if row else None


def list_all(
    db: Database,
    stage: str | None = None,
    archived: bool | None = None,
    applied: bool | None = None,
    tracker_archived: bool | None = None,
) -> list[Opportunity]:
    """Archived comes newest-archived first (spec 4.5); the rest newest-created first.
    The Tracker's tabs (applied with tracker_archived) come newest-applied first, and
    the Tracker's Archived tab newest-archived first."""
    if stage is not None and stage not in STAGES:
        raise ValueError(f"unknown stage {stage!r}")
    if tracker_archived is not None and applied is not True:
        raise ValueError("tracker_archived needs applied=true.")
    where, args = [], []
    if stage:
        where.append("stage = ?")
        args.append(stage)
    if archived is not None:
        where.append("archived = ?")
        args.append(int(archived))
    if applied is not None:
        where.append("application_status IS NOT NULL" if applied else "application_status IS NULL")
    if tracker_archived is not None:
        where.append("application_archived = ?")
        args.append(int(tracker_archived))
    sql = _SELECT
    if where:
        sql += " WHERE " + " AND ".join(where)
    if tracker_archived is not None:
        sql += " ORDER BY application_archived_at DESC, id" if tracker_archived else " ORDER BY applied_at DESC, id"
    else:
        sql += " ORDER BY archived_at DESC, id" if archived else " ORDER BY created_at DESC, id"
    with db.read() as conn:
        return [Opportunity.from_row(r) for r in conn.execute(sql, args)]


# --- writes (For You actions, no model call) --------------------------------
# Every function runs inside a caller-owned tx(). A bulk call is one operation:
# every id is checked first, and one bad id changes nothing (spec 3.4.5).

NOTES_MAX = 2_000
DESCRIPTION_MAX = 20_000
BULK_MAX = 500


def _fetch(conn: sqlite3.Connection, ids: list[str]) -> list[sqlite3.Row]:
    if not isinstance(ids, list) or not ids or len(ids) > BULK_MAX:
        raise ValueError(f"ids must be a list of 1 to {BULK_MAX} ids.")
    if not all(isinstance(i, str) for i in ids) or len(set(ids)) != len(ids):
        raise ValueError("ids must be unique strings.")
    rows = []
    for i in ids:
        row = conn.execute(
            "SELECT id, stage, archived, application_status, application_archived FROM opportunities WHERE id = ?", (i,)
        ).fetchone()
        if row is None:
            raise NotFound(f"No such opportunity: {i}")
        rows.append(row)
    return rows


def _return(conn: sqlite3.Connection, ids: list[str]) -> list[Opportunity]:
    return [Opportunity.from_row(conn.execute(_SELECT + " WHERE o.id = ?", (i,)).fetchone()) for i in ids]


def archive(conn: sqlite3.Connection, ids: list[str], now: str) -> list[Opportunity]:
    """Archive keeps stage and application: Unarchive returns to the same table."""
    for r in _fetch(conn, ids):
        if r["archived"]:
            raise Conflict(f"{r['id']} is already archived.")
        if r["stage"] == "filtered":
            raise Conflict(f"{r['id']} is filtered; promote it first.")
    conn.executemany("UPDATE opportunities SET archived = 1, archived_at = ? WHERE id = ?", [(now, i) for i in ids])
    return _return(conn, ids)


def unarchive(conn: sqlite3.Connection, ids: list[str]) -> list[Opportunity]:
    for r in _fetch(conn, ids):
        if not r["archived"]:
            raise Conflict(f"{r['id']} is not archived.")
    conn.executemany("UPDATE opportunities SET archived = 0, archived_at = NULL WHERE id = ?", [(i,) for i in ids])
    return _return(conn, ids)


def promote(conn: sqlite3.Connection, ids: list[str]) -> list[Opportunity]:
    """Filtered -> For Review. The filter reason no longer applies."""
    for r in _fetch(conn, ids):
        if r["stage"] != "filtered":
            raise Conflict(f"{r['id']} is not filtered.")
    conn.executemany(
        "UPDATE opportunities SET stage = 'for_review', filter_reason = NULL WHERE id = ?", [(i,) for i in ids]
    )
    return _return(conn, ids)


def set_saved(conn: sqlite3.Connection, ids: list[str], saved: object) -> list[Opportunity]:
    if not isinstance(saved, bool):
        raise ValueError("saved must be true or false.")
    _fetch(conn, ids)
    conn.executemany("UPDATE opportunities SET saved = ? WHERE id = ?", [(int(saved), i) for i in ids])
    return _return(conn, ids)


_PATCHABLE = {"description", "notes", "saved"}


def update_fields(conn: sqlite3.Connection, opportunity_id: str, changes: object) -> Opportunity:
    if not isinstance(changes, dict) or not changes:
        raise ValueError("Body must be a non-empty object.")
    unknown = set(changes) - _PATCHABLE
    if unknown:
        raise ValueError(f"Not editable: {', '.join(sorted(unknown))}.")
    (row,) = _fetch(conn, [opportunity_id])
    sets: dict[str, Any] = {}
    if "notes" in changes:
        sets["notes"] = sanitize.plain_text(changes["notes"], NOTES_MAX)
    if "description" in changes:
        if row["stage"] != "for_review":
            raise Conflict("The description is editable in For Review only.")
        sets["description"] = sanitize.plain_text(changes["description"], DESCRIPTION_MAX)
    if "saved" in changes:
        if not isinstance(changes["saved"], bool):
            raise ValueError("saved must be true or false.")
        sets["saved"] = int(changes["saved"])
    conn.execute(
        f"UPDATE opportunities SET {', '.join(f'{k} = ?' for k in sets)} WHERE id = ?",
        (*sets.values(), opportunity_id),
    )
    return _return(conn, [opportunity_id])[0]


# --- Tracker: Mark Applied, status, the Tracker's own archive ---------------
# `application` is not a lifecycle stage: an applied record stays in Analysed
# and appears in the Tracker, one object with two views. The Tracker's archive
# (application.archived) and For You's (archived) never touch each other.


def apply(conn: sqlite3.Connection, ids: list[str], now: str) -> list[Opportunity]:
    """Mark Applied: only an analysed, not archived record without an application.
    "Prepared" is not required and is not a status here (spec 3.13)."""
    for r in _fetch(conn, ids):
        if r["stage"] != "analysed":
            raise Conflict(f"{r['id']} is not analysed.")
        if r["archived"]:
            raise Conflict(f"{r['id']} is archived; unarchive it first.")
        if r["application_status"] is not None:
            raise Conflict(f"{r['id']} is already applied.")
    conn.executemany(
        "UPDATE opportunities SET application_status = 'applied', applied_at = ? WHERE id = ?", [(now, i) for i in ids]
    )
    return _return(conn, ids)


BACK_STATUSES = ("for_review", "analysed")


def set_status(conn: sqlite3.Connection, opportunity_id: str, status: object, now: str) -> Opportunity:
    """Set the status For You's Status dropdown chose.

    An application status: a record without an application gets one, with the
    same conditions as Mark Applied (analysed, not archived): choosing Approved
    or Declined for an Analysed row in For You is a Mark Applied that skips
    the "applied" step. An existing application only changes its status.

    For review or Analysed (dsgnrfhmn, 2026-10-25): back to that stage, see _move_back."""
    if status in BACK_STATUSES:
        return _move_back(conn, opportunity_id, status)
    if status not in APPLICATION_STATUSES:
        raise ValueError(f"status must be one of {', '.join(BACK_STATUSES + APPLICATION_STATUSES)}.")
    (row,) = _fetch(conn, [opportunity_id])
    if row["application_status"] is None:
        if row["stage"] != "analysed":
            raise Conflict(f"{opportunity_id} is not analysed: only an analysed record can take a status.")
        if row["archived"]:
            raise Conflict(f"{opportunity_id} is archived; unarchive it first.")
        conn.execute(
            "UPDATE opportunities SET application_status = ?, applied_at = ? WHERE id = ?",
            (status, now, opportunity_id),
        )
    else:
        conn.execute("UPDATE opportunities SET application_status = ? WHERE id = ?", (status, opportunity_id))
    return _return(conn, [opportunity_id])[0]


def _move_back(conn: sqlite3.Connection, opportunity_id: str, stage: str) -> Opportunity:
    """Back to For review or Analysed (dsgnrfhmn, 2026-10-25). Not archived.

    To Analysed (from an application status): the application is taken off, its status
    and date; its replies stay stored and show again once the record has an application
    again. The analysis is untouched.

    To For review: as above, and the analysis verdict goes (summary, eligible, relevance,
    details, its origin): the schema ties an analysis to the Analysed stage (CHECK stage =
    'analysed' OR analysis_origin IS NULL), and a summary without its origin could show a
    mock analysis as a real one. The facts it read off the call (type, deadline, the
    residency and decision dates) stay. Analyse brings it back; a live answer for the same
    text comes from the analysis cache (core/claude_client.py) at no cost.
    Filtered -> For review is the promote (the filter reason goes)."""
    (rec,) = _load(conn, [opportunity_id])
    if rec.archived:
        raise Conflict(f"{opportunity_id} is archived; unarchive it first.")
    if stage == "for_review" and rec.stage == "filtered":
        return promote(conn, [opportunity_id])[0]
    if stage == "analysed" and rec.stage != "analysed":
        raise Conflict(f"{opportunity_id} is not analysed: analyse it first.")
    off = ("application_status = NULL, applied_at = NULL, application_archived = 0,"
           " application_archived_at = NULL")
    if stage == "analysed":
        conn.execute(f"UPDATE opportunities SET {off} WHERE id = ?", (opportunity_id,))
    else:
        conn.execute(
            f"UPDATE opportunities SET stage = 'for_review', {off}, analysis_origin = NULL, summary = NULL,"
            " eligible = NULL, relevance = NULL, details = NULL WHERE id = ?",
            (opportunity_id,),
        )
    return _return(conn, [opportunity_id])[0]


def tracker_archive(conn: sqlite3.Connection, ids: list[str], now: str) -> list[Opportunity]:
    """Status is preserved. Only For You's flag is left alone, both ways."""
    for r in _fetch(conn, ids):
        if r["application_status"] is None:
            raise Conflict(f"{r['id']} has no application.")
        if r["application_archived"]:
            raise Conflict(f"{r['id']} is already archived in the Tracker.")
    conn.executemany(
        "UPDATE opportunities SET application_archived = 1, application_archived_at = ? WHERE id = ?",
        [(now, i) for i in ids],
    )
    return _return(conn, ids)


def tracker_unarchive(conn: sqlite3.Connection, ids: list[str]) -> list[Opportunity]:
    for r in _fetch(conn, ids):
        if r["application_status"] is None or not r["application_archived"]:
            raise Conflict(f"{r['id']} is not archived in the Tracker.")
    conn.executemany(
        "UPDATE opportunities SET application_archived = 0, application_archived_at = NULL WHERE id = ?",
        [(i,) for i in ids],
    )
    return _return(conn, ids)


# --- Analyse and Prepare: the writes after a model call ---------------------------
# The call happens first, outside any transaction (a slow API never holds the
# database). These functions check the state before it, so no money is spent on a
# record that cannot take the result, and again inside the write, so a change made
# meanwhile is refused instead of overwritten.


def _load(conn: sqlite3.Connection, ids: list[str]) -> list[Opportunity]:
    _fetch(conn, ids)
    return _return(conn, ids)


def analysable(conn: sqlite3.Connection, ids: list[str]) -> list[Opportunity]:
    """For Review, not archived, with text to analyse: a description, or the intake
    email text kept in `body` (spec 4.4.2)."""
    records = _load(conn, ids)
    for r in records:
        if r.stage != "for_review":
            raise Conflict(f"{r.id} is not in For Review.")
        if r.archived:
            raise Conflict(f"{r.id} is archived; unarchive it first.")
        if not r.description.strip() and not r.body.strip():
            raise Conflict(f"{r.id} has no description to analyse.")
    return records


def preparable(conn: sqlite3.Connection, ids: list[str]) -> list[Opportunity]:
    """Analysed, not archived, not yet prepared or applied (spec 4.3, 3.13)."""
    records = _load(conn, ids)
    for r in records:
        if r.stage != "analysed":
            raise Conflict(f"{r.id} is not analysed.")
        if r.archived:
            raise Conflict(f"{r.id} is archived; unarchive it first.")
        if r.application is not None:
            raise Conflict(f"{r.id} is already applied.")
        if r.prepared is not None:
            raise Conflict(f"{r.id} is already prepared.")
    return records


def record_analysis(conn: sqlite3.Connection, opportunity_id: str, fields: dict[str, Any], origin: str) -> Opportunity:
    """The row leaves For Review for Analysed, labelled with where the analysis came from."""
    (rec,) = analysable(conn, [opportunity_id])
    if origin not in ANALYSIS_ORIGINS:
        raise ValueError(f"origin must be one of {', '.join(ANALYSIS_ORIGINS)}.")
    conn.execute(
        "UPDATE opportunities SET stage = 'analysed', analysis_origin = ?, type = ?, summary = ?, eligible = ?,"
        " relevance = ?, deadline = ?, residency_start = ?, residency_end = ?, decision_date = ?, details = ?"
        " WHERE id = ?",
        (origin, fields["type"], fields["summary"], fields["eligible"], fields["relevance"], fields["deadline"],
         fields["residency_start"], fields["residency_end"], fields["decision_date"],
         json.dumps(fields["details"], ensure_ascii=False) if fields.get("details") else None, rec.id),
    )
    return _return(conn, [rec.id])[0]


def set_prepared(conn: sqlite3.Connection, opportunity_id: str, cv_set_id: str) -> Opportunity:
    (rec,) = preparable(conn, [opportunity_id])
    conn.execute("UPDATE opportunities SET prepared_cv_set_id = ? WHERE id = ?", (cv_set_id, rec.id))
    return _return(conn, [rec.id])[0]
