#!/usr/bin/env python3
"""One-off OAuth consent for arta's dedicated Google account.

Run by hand, once, on the machine arta runs on, after the OAuth client secret from
Google Cloud Console ("Clients" tab) is saved at google.client_secret_file (mode 600).
It writes the token to google.token_file (mode 600). This never runs as part of the
server, and no other module performs this flow.

It opens the consent page in this machine's browser; the redirect comes back to a
listener on localhost:8765 only. Sign in under the DEDICATED account (never a
personal one) and approve exactly two scopes: Gmail read-only, Drive read-only.

Prints no token, code or secret to the terminal.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from google_auth_oauthlib.flow import InstalledAppFlow  # noqa: E402

from core import config  # noqa: E402
from core.google_auth import SCOPES  # noqa: E402

PORT = 8765


def _paths() -> "tuple[Path, Path]":
    try:
        cfg = config.load()
    except config.ConfigError as e:
        raise SystemExit(f"google_auth_setup: {e}") from None
    secret, token = cfg.google_client_secret_file, cfg.google_token_file
    if secret is None or token is None:
        raise SystemExit("google_auth_setup: set google.client_secret_file and google.token_file in the configuration.")
    if not secret.is_file():
        raise SystemExit(
            f"No client secret at {secret}. Download it from Google Cloud Console -> "
            "Google Auth Platform -> Clients, save it there and chmod 600."
        )
    if secret.stat().st_mode & 0o077:
        raise SystemExit(f"{secret} is accessible to other users. Run: chmod 600 {secret}")
    return secret, token


def main() -> None:
    secret_path, out_path = _paths()

    if out_path.exists():
        answer = input(f"{out_path} already exists. Overwrite? [y/N] ").strip().lower()
        if answer != "y":
            print("Left the existing token in place. Nothing written.")
            return

    flow = InstalledAppFlow.from_client_secrets_file(str(secret_path), SCOPES)

    print("Sign in under the DEDICATED arta account and approve the two read-only scopes.\n")

    creds = flow.run_local_server(
        host="localhost",
        port=PORT,
        open_browser=True,
        authorization_prompt_message="Visit this URL to authorize arta:\n{url}\n",
        success_message="arta is authorized. Close this tab and return to the terminal.",
    )

    granted = set(creds.scopes or [])
    missing = set(SCOPES) - granted
    if missing:
        raise SystemExit(
            f"Consent completed but {sorted(missing)} was not granted. "
            "Re-run and approve both checkboxes; nothing was saved."
        )

    out_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Created 0600 from the start, never briefly readable by others.
    fd = os.open(out_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(creds.to_json())
    out_path.chmod(0o600)
    print(f"Saved token to {out_path} (mode 0600). Granted: {sorted(granted)}")


if __name__ == "__main__":
    main()
