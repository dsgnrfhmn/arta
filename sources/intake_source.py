"""The IntakeSource interface: where "Load opportunities" gets new items from.

"Load opportunities" pulls from every connected source, not only Gmail (a
Telegram bot or others may follow), so intake iterates a list of sources behind
this one interface and reports per source.

Read-only, like every arta mail access (CLAUDE.md, Security): a source lists and
reads and never marks, labels, moves or sends anything. What has been seen lives
in the `intake_seen` table, not in the source.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class IntakeError(Exception):
    """The source cannot be read. The message is user-facing, and never holds a token."""


@dataclass(frozen=True)
class IncomingOpportunity:
    message_id: str    # stable id from the source; the seen-set key (per source)
    sender: str        # who it came from, as a short display string
    subject: str       # becomes the record's name
    received_at: str   # ISO 8601, UTC
    body: str          # raw text: pipeline/intake.py sanitizes before it is stored
    link: str = ""     # untrusted URL of the item itself; pipeline/intake.py validates it, "" for none
    description: str = ""  # the item's own text, shown as Full Description; sanitized by pipeline/intake.py
    deadline: str = ""     # ISO date or datetime if the source states one; pipeline/intake.py keeps the date part
    sender_address: str = ""  # bare address if the source has one ("" otherwise); only pipeline/intake_filter.py reads it


class IntakeSource(Protocol):
    name: str          # short and stable ("gmail", "fixtures"); prefixes ids in intake_seen
    # Optional, default False: True when the source is the mailbox Updates reads, so
    # pipeline/intake.py skips replies to applied items.

    def fetch(self, seen: set[str]) -> list[IncomingOpportunity]:
        """Items whose message_id is not in `seen`. Raises IntakeError."""
