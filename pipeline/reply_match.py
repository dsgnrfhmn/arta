"""Which applied opportunity a message replies to. Pure: no mailbox, no db.

Used by sources/gmail_mail_updates.py to assign replies, and by pipeline/intake.py
to keep those same replies out of For Review.

Rule (dsgnrfhmn, 2026-10-23): a message belongs to an application when the
opportunity's name is in it: the sender's display name, the subject and the body
together. A name matches when at least NAME_THRESHOLD of its distinctive words
are there (words every open call shares, and numbers, do not count). Only
messages received on or after the application date count. A message that fits
several applications goes to the one with the highest score; a tie is not
assigned.

Known limit: a newsletter that advertises an applied call again also contains its
name, after the application date, and is assigned as a reply.
"""

from __future__ import annotations

import re

from models.opportunity import Opportunity

# A name matches when this share of its distinctive words is in the message
# (job-agent's read_email_updates uses the same gate).
NAME_THRESHOLD = 0.6

# Words every open call shares; they say nothing about which one a reply is about.
_GENERIC = frozenset({
    "the", "and", "of", "for", "in", "a", "an", "to", "at", "with", "on", "by", "from",
    "open", "call", "calls", "application", "applications", "apply", "program", "programme",
    "residency", "residencies", "grant", "grants", "fellowship", "award", "prize", "fund",
    "artist", "artists", "art", "arts", "international", "deadline", "new", "re", "fwd", "fw",
})
_WORD = re.compile(r"[^\W_]+", re.UNICODE)


def _words(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.casefold()) if w not in _GENERIC and not w.isdigit()}


def candidates(sender_name: str, subject: str, body: str, received_at: str,
               applied: list[Opportunity]) -> list[Opportunity]:
    """The best-fitting applications for this message: none, one, or a tie."""
    best: tuple[float, list[Opportunity]] = (0.0, [])
    words = _words(f"{sender_name} {subject} {body}")
    for o in applied:
        a = o.application
        if a is None or received_at[:10] < a.applied_at[:10]:
            continue
        want = _words(o.name)
        score = len(want & words) / len(want) if want else 0.0
        if score < NAME_THRESHOLD:
            continue
        if score > best[0]:
            best = (score, [o])
        elif score == best[0]:
            best[1].append(o)
    return best[1]


def match(sender_name: str, subject: str, body: str, received_at: str,
          applied: list[Opportunity]) -> Opportunity | None:
    """The application this message replies to, or None (no fit, or a tie)."""
    found = candidates(sender_name, subject, body, received_at, applied)
    return found[0] if len(found) == 1 else None
