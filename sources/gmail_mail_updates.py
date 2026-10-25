"""MailUpdates over the dedicated Gmail account. Read-only: gmail.readonly scope,
nothing here marks, labels, moves or sends (core/google_auth.py owns the credentials;
this module only gets a client from it). Replies reach this mailbox because the artist
applies from it or forwards replies to it (dsgnrfhmn, 2026-10-23).

A message is assigned to an application when the opportunity's name is in its
sender, subject or body, on or after the application date; the rule and its known
limit are in pipeline/reply_match.py.

Log lines carry message ids, never senders, subjects or bodies.
"""

from __future__ import annotations

import logging
from email.utils import parseaddr
from typing import Any, Callable

from models.opportunity import Opportunity
from pipeline.reply_match import candidates
from sources.mail_updates import IncomingUpdate, MailError
from sources.opencall_email_source import _received, _sender, message_text

log = logging.getLogger("arta.updates")

# One Refresh reads at most this many inbox messages since the oldest application;
# the API lists newest first, so the cut drops the oldest.
MAX_SCAN = 500


class GmailMailUpdates:
    name = "gmail"

    def __init__(self, service_factory: "Callable[[], Any]"):
        self._factory = service_factory

    def _service(self):
        return self._factory()

    def fetch(self, applied: list[Opportunity]) -> list[IncomingUpdate]:
        applied = [o for o in applied if o.application is not None]
        if not applied:
            return []
        since = min(o.application.applied_at[:10] for o in applied).replace("-", "/")
        try:
            svc = self._service()
            ids: list[str] = []
            token = None
            while len(ids) < MAX_SCAN:
                r = svc.users().messages().list(
                    userId="me", q=f"in:inbox after:{since}", maxResults=min(500, MAX_SCAN), pageToken=token,
                ).execute()
                ids += [m["id"] for m in r.get("messages", [])]
                token = r.get("nextPageToken")
                if not token:
                    break
            out = []
            for message_id in ids[:MAX_SCAN]:
                m = svc.users().messages().get(userId="me", id=message_id, format="full").execute()
                payload = m.get("payload", {})
                headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}
                sender_name = parseaddr(headers.get("from", ""))[0]
                subject, body = headers.get("subject", ""), message_text(payload)
                received = _received(m.get("internalDate"))
                found = candidates(sender_name, subject, body, received, applied)
                if len(found) > 1:
                    log.info("updates gmail: message %s fits %s equally, not assigned",
                             message_id, ", ".join(o.id for o in found))
                if len(found) != 1:
                    continue
                hit = found[0]
                out.append(IncomingUpdate(message_id, hit.id, _sender(headers.get("from", "")), received, subject, body))
                log.info("updates gmail: message %s matched %s", message_id, hit.id)
            return out
        except MailError:
            raise
        except Exception as e:  # noqa: BLE001 -- GoogleAuthError, HttpError, network: all become one user-facing error
            if type(e).__name__ == "GoogleAuthError":   # its message is user-facing and never holds a token
                raise MailError(str(e)) from None
            # Anything else: the class name only, so neither message nor traceback can carry a token.
            raise MailError(f"The mailbox could not be read ({type(e).__name__}). Nothing was changed.") from None
