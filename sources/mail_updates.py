"""The MailUpdates interface: replies that belong to applied opportunities.

Read-only, like every arta mail access (CLAUDE.md, Security): the source lists
messages and never marks, labels, moves or sends anything. What has been seen
lives in the `updates` table (models/update.py), not in the mailbox.

Matching a message to an application is the source's job: it is handed the
applied records and returns only messages that belong to one of them, each with
that record's id. Gmail matches by the opportunity's name in the message
(pipeline/reply_match.py); the fixture names the record id in each message.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from models.opportunity import Opportunity


class MailError(Exception):
    """The mailbox cannot be read. The message is user-facing, and never holds a token."""


@dataclass(frozen=True)
class IncomingUpdate:
    message_id: str       # stable identifier from the mailbox; the seen-set key
    opportunity_id: str
    sender: str
    received_at: str
    subject: str
    body: str             # raw: models/update.py sanitizes before it is stored


class MailUpdates(Protocol):
    def fetch(self, applied: list[Opportunity]) -> list[IncomingUpdate]:
        """Every message that matches one of `applied` (archived ones included).
        Raises MailError."""
