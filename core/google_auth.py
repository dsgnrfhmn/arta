"""core/google_auth.py — the one place holding the dedicated Google account.

Owns (CLAUDE.md, "Credentials and external services"):
- credential loading from outside the repo tree, mode 0600
- the two read-only scopes: Gmail read, Drive read — nothing else, ever
- Gmail and Drive client construction

Nothing else in the codebase touches credentials. If a write scope ever
looks necessary, that is a design conversation to raise with dsgnrfhmn, not a
scope to quietly add here.

Fail loud: a missing, unreadable, too-open or unrefreshable token is an
exception, never a silent fall-through to a mock, a cache, or "no auth".
"""

from __future__ import annotations

import stat
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import Resource, build

# Read-only, both of them. Restrict-by-sharing does the rest: the artist
# shares the project folder *to* this account, so even the broad-looking
# drive.readonly scope only ever sees that one folder.
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
]


class GoogleAuthError(Exception):
    """The dedicated account's credentials are missing, invalid or exposed.

    The message is user-facing and never includes the token or secret
    itself — only the path, per CLAUDE.md ("never echo a token").
    """


def _require_private(path: Path) -> None:
    """Refuse a credential file that group or other can read.

    A scope that was requested but not granted fails quietly at first
    use (CLAUDE.md, "Verify, do not assume"); a permission mistake on the
    token file must not fail quietly the same way.
    """
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise GoogleAuthError(f"{path} is not private (mode {oct(mode)}). Run: chmod 600 {path}")


def load_credentials(token_file: Path) -> Credentials:
    """Load the stored token for the dedicated account, refreshing if expired.

    Never falls back to anything else: a revoked or missing token must
    make every caller fail closed, not serve a stale cache as if it were
    live (CLAUDE.md, "confirm the app fails closed when it is revoked").
    """
    path = token_file
    if not path.exists():
        raise GoogleAuthError(
            f"No token at {path}. Run scripts/google_auth_setup.py once to "
            "complete the OAuth consent for the dedicated account."
        )
    _require_private(path)

    creds = Credentials.from_authorized_user_file(str(path), SCOPES)

    if creds.valid:
        return creds

    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        path.write_text(creds.to_json())
        path.chmod(0o600)
        return creds

    raise GoogleAuthError(
        f"Token at {path} is invalid and cannot be refreshed (revoked or "
        "denied). Re-run scripts/google_auth_setup.py to reconnect the "
        "account; nothing here falls back to cached or mock data."
    )


def gmail_service(token_file: Path) -> Resource:
    """Read-only Gmail client for the dedicated account (google.token_file)."""
    return build("gmail", "v1", credentials=load_credentials(token_file), cache_discovery=False)


def drive_service(token_file: Path) -> Resource:
    """Read-only Drive client for the dedicated account (google.token_file)."""
    return build("drive", "v3", credentials=load_credentials(token_file), cache_discovery=False)
