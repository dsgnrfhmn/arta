"""Intake: new items from every connected source become For Review records.

One item is one record: stage for_review, type open_call (analysis may correct
it), name = subject, description empty unless the source has its own text
(a website listing does), the sanitized text kept in `body`
for analysis. When filters are given (pipeline/intake_filter.py: sender and
subject, never the body) a matched item is stored the same way but in stage
filtered with the rule's reason; without filters nothing is filtered. A filtered
item is marked seen too, so it is not judged again; promote it from Filtered. A
dropped item (a "drop" rule) is not stored at all, only marked seen.

A source that reads the same mailbox as Updates (`mailbox = True`) also gets the
applied records: a message that pipeline/reply_match.py assigns to one of them is a
reply, shown under that record's Updates, so intake marks it seen and stores no
For Review record for it (dsgnrfhmn, 2026-10-23).

Deduplication is the `intake_seen` table, keyed "<source>:<message id>", written
in the same transaction as the record: an item is either stored and marked seen,
or neither. Running twice adds nothing the second time.

Items are untrusted: subject and body pass through core/sanitize before they are
stored, and a link is stored only when it is a plain https URL (see _link); an
invalid one is dropped and the record is kept without it. Links are data: they are
never fetched here. Log lines carry message ids, never subjects, bodies or URLs.
"""

from __future__ import annotations

import datetime
import logging
import re
import secrets
from typing import Any, Sequence
from urllib.parse import urlsplit

from core import sanitize
from core.db import Database
from models import opportunity
from pipeline.intake_filter import IntakeFilters
from pipeline.reply_match import match as reply_to
from sources.intake_source import IncomingOpportunity, IntakeSource

log = logging.getLogger("arta.intake")

NAME_MAX = 200
BODY_MAX = 100_000
LINK_MAX = 2048
DESCRIPTION_MAX = 20_000
_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:[T ].*)?$")


def _name(subject: str) -> str:
    try:
        return sanitize.plain_text(subject, NAME_MAX) or "(no subject)"
    except ValueError:
        return sanitize.plain_text(subject[:NAME_MAX], NAME_MAX) or "(no subject)"


def _body(text: str) -> str:
    try:
        return sanitize.plain_text(text[:BODY_MAX], BODY_MAX)
    except ValueError:
        return ""


def _link(url: str) -> str | None:
    """The URL when it is a plain https link, else None. Rejects other schemes
    (javascript:, data:, http:), credentials in the URL, whitespace and control
    characters, a missing host, and anything over LINK_MAX."""
    if not isinstance(url, str) or not url or len(url) > LINK_MAX:
        return None
    if any(ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in url):
        return None
    try:
        parts = urlsplit(url)
        host, port = parts.hostname, parts.port
    except ValueError:
        return None
    if parts.scheme != "https" or not host or parts.username is not None or parts.password is not None:
        return None
    if port not in (None, 443):
        return None
    return url


def _description(text: str) -> str:
    try:
        return sanitize.plain_text(text[:DESCRIPTION_MAX], DESCRIPTION_MAX)
    except ValueError:
        return ""


def _deadline(value: str) -> str | None:
    """The date part (YYYY-MM-DD) of an ISO date or datetime, if it is a real date."""
    m = _DATE.match(value or "")
    if not m:
        return None
    try:
        datetime.date(int(m[1]), int(m[2]), int(m[3]))
    except ValueError:
        return None
    return f"{m[1]}-{m[2]}-{m[3]}"


def _source(sender: str) -> str:
    try:
        return sanitize.plain_text(sender, 120) or "unknown sender"
    except ValueError:
        return "unknown sender"


def _seen(db: Database, source: str) -> set[str]:
    prefix = f"{source}:"
    with db.read() as c:
        return {r[0][len(prefix):] for r in c.execute("SELECT message_id FROM intake_seen WHERE message_id LIKE ?", (prefix + "%",))}


def _mark_seen(db: Database, source: str, item: IncomingOpportunity, now: str) -> None:
    with db.tx() as conn:
        conn.execute("INSERT INTO intake_seen (message_id, seen_at) VALUES (?, ?)", (f"{source}:{item.message_id}", now))


def _store(db: Database, source: str, item: IncomingOpportunity, now: str, filter_reason: "str | None" = None) -> None:
    link = _link(item.link) if item.link else None
    if item.link and link is None:
        log.warning("intake %s: dropped an invalid link on message %s", source, item.message_id)
    rec = opportunity.Opportunity(
        id=secrets.token_hex(8), stage="filtered" if filter_reason else "for_review", name=_name(item.subject), type="open_call",
        source=_source(item.sender), created_at=item.received_at or now, description=_description(item.description),
        body=_body(item.body), link=link, deadline=_deadline(item.deadline),
        filter_reason=filter_reason,
    )
    with db.tx() as conn:
        opportunity.insert(conn, rec)
        conn.execute("INSERT INTO intake_seen (message_id, seen_at) VALUES (?, ?)", (f"{source}:{item.message_id}", now))


def run(
    db: Database, sources: Sequence[IntakeSource], now: str, filters: "IntakeFilters | None" = None,
    applied: "Sequence[opportunity.Opportunity]" = (),
) -> dict[str, Any]:
    """{added, filtered, sources: [{name, added, filtered}]}. A source that cannot be
    read raises IntakeError and stops the run; what earlier sources stored stays."""
    report = []
    for src in sources:
        items = src.fetch(_seen(db, src.name))
        replies_to = list(applied) if getattr(src, "mailbox", False) else []
        filtered = dropped = replies = 0
        for item in items:
            hit = reply_to(item.sender, item.subject, item.body, item.received_at, replies_to) if replies_to else None
            if hit is not None:
                _mark_seen(db, src.name, item, now)
                replies += 1
                log.info("intake %s: message %s is a reply to %s, left to Updates", src.name, item.message_id, hit.id)
                continue
            verdict = filters.check(item.sender_address, item.subject) if filters else None
            if verdict is not None and verdict.action == "drop":
                _mark_seen(db, src.name, item, now)
                dropped += 1
                log.info("intake %s: dropped message %s (%s)", src.name, item.message_id, verdict.reason)
                continue
            reason = verdict.reason if verdict else None
            _store(db, src.name, item, now, reason)
            filtered += bool(reason)
            if reason:
                log.info("intake %s: filtered message %s (%s)", src.name, item.message_id, reason)
            else:
                log.info("intake %s: stored message %s", src.name, item.message_id)
        entry = {"name": src.name, "added": len(items) - filtered - dropped - replies, "filtered": filtered}
        if dropped:
            entry["dropped"] = dropped      # a count only; the item itself is nowhere
        if replies:
            entry["replies"] = replies      # a count only; Updates stores the message itself
        notes = list(getattr(src, "notes", []) or [])
        if notes:
            entry["notes"] = notes      # e.g. layout drift; only present when there is something to say
        report.append(entry)
    result = {
        "added": sum(r["added"] for r in report), "filtered": sum(r["filtered"] for r in report), "sources": report,
    }
    dropped = sum(r.get("dropped", 0) for r in report)
    if dropped:
        result["dropped"] = dropped
    return result
