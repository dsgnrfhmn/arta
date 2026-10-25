#!/usr/bin/env python3
"""Exercise the dedicated account's three accesses separately.

"The consent screen was accepted" is not verification (CLAUDE.md). This
prints only message/file identifiers and counts, never bodies or content
(CLAUDE.md: "Log message identifiers, not message bodies").

Also confirms a write attempt fails: the token carries only read-only
scopes, so both write calls below are expected to be rejected by Google
before anything changes — this is the API refusing, not code choosing not
to try.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from googleapiclient.errors import HttpError  # noqa: E402

from core import config  # noqa: E402
from core.google_auth import drive_service, gmail_service  # noqa: E402

TOKEN: Path | None = None   # google.token_file, set in main() from the configuration


def check_gmail_read() -> str | None:
    print("== Gmail: list messages ==")
    svc = gmail_service(TOKEN)
    resp = svc.users().messages().list(userId="me", maxResults=5).execute()
    messages = resp.get("messages", [])
    print(f"  {len(messages)} message id(s) returned (of {resp.get('resultSizeEstimate', '?')} total):")
    for m in messages:
        print(f"    id={m['id']}")
    return messages[0]["id"] if messages else None


def check_drive_read() -> str | None:
    print("== Drive: list visible files/folders ==")
    svc = drive_service(TOKEN)
    resp = svc.files().list(pageSize=25, fields="files(id,name,mimeType)").execute()
    files = resp.get("files", [])
    print(f"  {len(files)} item(s) visible:")
    for f in files:
        kind = "folder" if f["mimeType"] == "application/vnd.google-apps.folder" else "file"
        print(f"    [{kind}] {f['name']} (id={f['id']})")
    print(
        "  Confirm by eye: this list should be ONLY the artist's shared project "
        "folder (and its contents if Drive expands it), nothing else in the "
        "dedicated account's own Drive."
    )
    return files[0]["id"] if files else None


def check_gmail_write_fails(message_id: str | None) -> None:
    print("== Gmail: confirm a write is rejected ==")
    if not message_id:
        print("  skipped: no message to try it on.")
        return
    svc = gmail_service(TOKEN)
    try:
        svc.users().messages().modify(
            userId="me", id=message_id, body={"removeLabelIds": ["UNREAD"]}
        ).execute()
        print("  UNEXPECTED: the write call succeeded. Scope is not read-only. Stop and check the token.")
    except HttpError as exc:
        print(f"  expected failure: HTTP {exc.resp.status} — {exc.reason}")


def check_drive_write_fails(file_id: str | None) -> None:
    print("== Drive: confirm a write is rejected ==")
    if not file_id:
        print("  skipped: no file to try it on.")
        return
    svc = drive_service(TOKEN)
    try:
        svc.files().update(fileId=file_id, body={"name": "arta-write-test"}).execute()
        print("  UNEXPECTED: the write call succeeded. Scope is not read-only. Stop and check the token.")
    except HttpError as exc:
        print(f"  expected failure: HTTP {exc.resp.status} — {exc.reason}")


def main() -> None:
    global TOKEN
    try:
        TOKEN = config.load().google_token_file
    except config.ConfigError as e:
        raise SystemExit(f"google_auth_verify: {e}") from None
    if TOKEN is None:
        raise SystemExit("google_auth_verify: google.token_file is not set in the configuration.")
    msg_id = check_gmail_read()
    print()
    file_id = check_drive_read()
    print()
    check_gmail_write_fails(msg_id)
    print()
    check_drive_write_fails(file_id)


if __name__ == "__main__":
    main()
