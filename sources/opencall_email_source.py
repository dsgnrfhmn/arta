"""IntakeSource over the dedicated Gmail account. Read-only: gmail.readonly
scope, nothing here marks, labels, moves or sends (core/google_auth.py owns the
credentials; this module only gets a client from it).

One message is one item: the subject is the name, the text is the body. Links are
not extracted or followed (newsletter links are click-tracking redirects, and
arta never fetches a link from an email). Log lines carry message ids, never
bodies or subjects.
"""

from __future__ import annotations

import base64
import logging
from datetime import datetime, timezone
from email.utils import parseaddr
from html.parser import HTMLParser
from typing import Any, Callable

from sources.intake_source import IncomingOpportunity, IntakeError

log = logging.getLogger("arta.intake")

# One run reads at most this many unseen messages, newest first; the rest wait
# for the next run (they are not marked, they simply stay unseen).
MAX_PER_RUN = 200


class _Html(HTMLParser):
    """Visible text of an HTML part; scripts and styles are dropped."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in ("br", "p", "div", "tr", "li", "h1", "h2", "h3", "h4"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def _walk(part: dict) -> "Any":
    yield part
    for child in part.get("parts") or []:
        yield from _walk(child)


def _decode(part: dict) -> str:
    data = (part.get("body") or {}).get("data")
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", "replace") if data else ""


def message_text(payload: dict) -> str:
    """The plain-text part if there is one, else the visible text of the HTML part."""
    parts = list(_walk(payload))
    for want in ("text/plain", "text/html"):
        for p in parts:
            if p.get("mimeType") == want and _decode(p):
                if want == "text/plain":
                    return _decode(p)
                h = _Html()
                h.feed(_decode(p))
                return "".join(h.parts)
    return ""


def _sender(raw: str) -> str:
    name, addr = parseaddr(raw)
    return name or addr or "unknown sender"


def _received(internal_date_ms: str | None) -> str:
    try:
        return datetime.fromtimestamp(int(internal_date_ms) / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError):
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class GmailIntake:
    name = "gmail"
    mailbox = True     # the mailbox Updates reads: pipeline/intake.py leaves replies to applied items to it

    def __init__(self, service_factory: "Callable[[], Any]"):
        self._factory = service_factory

    def _service(self):
        return self._factory()

    def fetch(self, seen: set[str]) -> list[IncomingOpportunity]:
        try:
            svc = self._service()
            ids: list[str] = []
            token = None
            while True:
                r = svc.users().messages().list(userId="me", maxResults=500, pageToken=token).execute()
                ids += [m["id"] for m in r.get("messages", [])]
                token = r.get("nextPageToken")
                if not token:
                    break
            fresh = [i for i in ids if i not in seen][:MAX_PER_RUN]   # the API lists newest first
            out = []
            for message_id in fresh:
                m = svc.users().messages().get(userId="me", id=message_id, format="full").execute()
                headers = {h["name"].lower(): h["value"] for h in m["payload"].get("headers", [])}
                out.append(IncomingOpportunity(
                    message_id, _sender(headers.get("from", "")), headers.get("subject", ""),
                    _received(m.get("internalDate")), message_text(m["payload"]),
                    sender_address=parseaddr(headers.get("from", ""))[1],
                ))
                log.info("intake gmail: read message %s", message_id)
            return out
        except IntakeError:
            raise
        except Exception as e:  # noqa: BLE001 -- GoogleAuthError, HttpError, network: all become one user-facing error
            if type(e).__name__ == "GoogleAuthError":   # its message is user-facing and never holds a token
                raise IntakeError(str(e)) from None
            # Anything else: the class name only, so neither message nor traceback can carry a token.
            raise IntakeError(f"The mailbox could not be read ({type(e).__name__}). Nothing was changed.") from None
