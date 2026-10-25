"""IntakeSource over tests/fixtures/emails.json (intake.sources = ["fixtures"]). Synthetic
emails, two of them adversarial; no network, no token."""

from __future__ import annotations

import json
from pathlib import Path

from sources.intake_source import IncomingOpportunity, IntakeError


class FixtureIntake:
    name = "fixtures"

    def __init__(self, path: Path):
        self.path = Path(path)

    def fetch(self, seen: set[str]) -> list[IncomingOpportunity]:
        try:
            emails = json.loads(self.path.read_text("utf-8"))
        except (OSError, ValueError) as e:
            raise IntakeError(f"The fixture emails could not be read: {e}") from None
        return [
            IncomingOpportunity(e["id"], e["from"], e["subject"], e.get("received_at", "2026-09-01T00:00:00Z"), e["body"],
                               sender_address=e["from"] if "@" in e["from"] else "")
            for e in emails if e["id"] not in seen
        ]
