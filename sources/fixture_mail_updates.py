"""MailUpdates over tests/fixtures/updates.json, selected only by
updates.source = "fixtures". A synthetic mailbox, never a source of truth.

Each message names the id of the opportunity it replies to (names are not unique). A message for an opportunity
that has no application yet is not returned; it is picked up on the Refresh
after the record is applied. Refresh reading the same mailbox twice returns the
same messages, and the `updates` table's seen-set keeps them from being stored
twice, exactly as it will with Gmail.
"""

from __future__ import annotations

import json
from pathlib import Path

from models.opportunity import Opportunity
from sources.mail_updates import IncomingUpdate, MailError

_FIELDS = {"message_id", "opportunity_id", "sender", "received_at", "subject", "body", "seed"}


class FixtureMailUpdates:
    def __init__(self, path: Path):
        self.messages: list[dict] = json.loads(Path(path).read_text("utf-8"))
        seen: set[str] = set()
        for m in self.messages:
            if set(m) != _FIELDS:
                raise MailError(f"Mail fixture: a message has fields {sorted(m)}, expected {sorted(_FIELDS)}.")
            if m["message_id"] in seen:
                raise MailError(f"Mail fixture: message id {m['message_id']} appears twice.")
            seen.add(m["message_id"])

    def fetch(self, applied: list[Opportunity]) -> list[IncomingUpdate]:
        ids = {o.id for o in applied}
        return [
            IncomingUpdate(m["message_id"], m["opportunity_id"], m["sender"], m["received_at"], m["subject"], m["body"])
            for m in self.messages
            if m["opportunity_id"] in ids
        ]
