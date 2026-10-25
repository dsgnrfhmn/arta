"""The pages' files and the browser-facing security headers, one source for both servers.

The API server (web/server.py) serves the pages from www/ and the design system from
dis-system/ under /dis-system/; the PDF export's print server (web/pdf_export.py) serves
the same files to headless Chromium. Both send SECURITY_HEADERS with every response.

  CSP: scripts, styles, images, fonts and fetches only from this origin; no inline
    script (the pages load every script from a file), so markup that slipped into a
    page from an email could not run. Inline styles stay allowed: page <style> blocks
    hold layout, and a style cannot run code.
  frame-ancestors / X-Frame-Options: the dashboard cannot be framed by another site.
  no-referrer: a link opened from an open call does not tell that site the address.
  nosniff: a file is never run as a type other than the one it is served as.
"""

from __future__ import annotations

import mimetypes
from pathlib import Path

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self'; "
    "font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; "
    "frame-ancestors 'none'"
)
SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
    "Cross-Origin-Opener-Policy": "same-origin",
}
INDEX = "for-you.html"

mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("font/woff2", ".woff2")
mimetypes.add_type("application/json", ".json")


def resolve(repo: Path, path: str) -> Path | None:
    """The file an (unquoted) URL path names, or None.

    Only regular files under www/, or under dis-system/ for /dis-system/...; "/" is the
    index page. No dotfile or dot-directory, and nothing that resolves outside its root
    (.., a symlink pointing elsewhere)."""
    parts = [p for p in path.split("/") if p]
    if not parts:
        parts = [INDEX]
    if parts[0] == "dis-system":
        base, rel = (repo / "dis-system").resolve(), parts[1:]
    else:
        base, rel = (repo / "www").resolve(), parts
    if not rel or any(p.startswith(".") for p in rel):
        return None
    target = base.joinpath(*rel).resolve()
    if base not in target.parents or not target.is_file():
        return None
    return target


def content_type(target: Path) -> str:
    ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    if ctype.startswith("text/") or ctype in ("application/json", "image/svg+xml"):
        ctype += "; charset=utf-8"
    return ctype
