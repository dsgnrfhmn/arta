"""PDF export of a portfolio or CV set (dsgnrfhmn, 2026-10-24): the same pages as the preview.

The sheets are drawn by the pages' own code (Portfolio's sheets, CV's cv-sheet), so the
PDF is that drawing printed, not a second layout that could drift from it. render() runs
in the export worker (web/jobs.py) and does three things:

1. Starts a print server on 127.0.0.1, on a free port, for this one export. It serves
   www/ and dis-system/ from the repository, the API's GET routes (the same handlers,
   over the worker's own Context) and /api/print/images/<project>/<image>?px=: the
   original from the project source, read now, scaled to the pixels the page asks for
   (the long side its place on the sheet needs at 300 dpi, arta-print.js) and never
   stored (the thumbnail cache keeps thumbnails only). Every request must carry this
   export's one-time token; the server answers nothing else and only GET. It sends the
   dashboard's security headers (web/static.py), so the page runs under the same rules
   as in the browser.
2. Opens <page>.html?set=<id>&print=1 in headless Chromium (Playwright). Chromium can
   reach the print server only: every other request is aborted, so nothing leaves the
   machine. In print mode the page (www/arta-print.js) draws the set as usual, swaps each
   thumbnail for its original sized to its place, then keeps only its sheets at their own
   size, 842 x 599 px, one per PDF page, and reports ready or an error.
3. Prints that to out as a PDF, page size 842 x 599 pt (the artist's own PDF's), at the
   scale that makes one 842 px sheet one page, backgrounds on.

Progress goes to the progress callback whenever it changes: {"phase": "drawing"}, then
{"phase": "pictures", "done": n, "total": m} as the originals arrive (the page counts
them), then {"phase": "printing"}.

Failures the artist can act on raise ExportError with a message for her.
"""

from __future__ import annotations

import hmac
import logging
import re
import secrets
import threading
import time
from dataclasses import replace
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, quote, unquote, urlsplit

from core.config import REPO_ROOT

log = logging.getLogger("arta.pdf")

PAGES = {"portfolio": "portfolio.html", "cv": "cv.html"}
# One sheet is 842 x 599 CSS px (the preview's own units); the PDF page is 842 x 599 pt
# (arta.css @page, the size of the artist's own PDF). Chromium prints 1 px as 0.75 pt, so
# the page is printed at 4/3 to map the sheet onto the page exactly.
SCALE = 4 / 3
MIN_PX, MAX_PX = 200, 3600           # long side of a picture in the PDF (3600: a full sheet at ~300 dpi)
READY_TIMEOUT_MS = 900_000           # drawing the set, every original read from Drive included
TOKEN_HEADER = "X-Arta-Print"
_IMAGE = re.compile(r"^/api/print/images/([^/]+)/([^/]+)$")


class ExportError(Exception):
    """The PDF could not be made. The message is user-facing."""


class ExportStopped(ExportError):
    """The reader stopped the export (web/exports.py, cancel)."""


def _handler(ctx: Any, token: str, repo: Path) -> type:
    from sources.thumb_cache import ThumbError, print_image
    from sources.project_source import NotFound, SourceError
    from web import server, static

    api = server.make_handler(replace(ctx, jobs=None), server.build_router())

    class PrintHandler(api):
        def _plain(self, status: int, data: bytes = b"", ctype: str = "text/plain; charset=utf-8") -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            for k, v in static.SECURITY_HEADERS.items():
                self.send_header(k, v)
            self.end_headers()
            if data:
                self.wfile.write(data)

        def _allowed(self) -> bool:
            if hmac.compare_digest(self.headers.get(TOKEN_HEADER, ""), token):
                return True
            self.close_connection = True
            self._plain(403)
            return False

        def do_GET(self) -> None:
            if not self._allowed():
                return
            path = urlsplit(self.path).path
            image = _IMAGE.match(path)
            if image:
                px = parse_qs(urlsplit(self.path).query).get("px", [""])[-1]
                self._image(unquote(image[1]), unquote(image[2]), px)
            elif path.startswith("/api/"):
                super().do_GET()
            else:
                self._static(unquote(path))

        def _image(self, project_id: str, image_id: str, px: str) -> None:
            size = min(MAX_PX, max(MIN_PX, int(px))) if px.isdigit() else MAX_PX
            try:
                data = print_image(ctx.projects.read_image(project_id, image_id), size)
            except NotFound:
                self._plain(404)
                return
            except (SourceError, ThumbError) as e:
                log.warning("pdf: picture %s/%s not drawn: %s", project_id, image_id, e)
                self._plain(502)
                return
            self._plain(200, data, "image/jpeg")

        def _static(self, path: str) -> None:
            target = static.resolve(repo, path)
            if target is None:
                self._plain(404)
                return
            self._plain(200, target.read_bytes(), static.content_type(target))

        def _refuse(self) -> None:
            if self._allowed():
                self._plain(405)

        do_POST = do_PUT = do_PATCH = do_DELETE = _refuse

    return PrintHandler


POLL_MS = 400


def render(ctx: Any, kind: str, set_id: str, out: Path, repo: Path = REPO_ROOT,
           progress: "Callable[[dict], None] | None" = None,
           stopped: "Callable[[], bool] | None" = None) -> int:
    """Print the set to out (PDF). Returns the number of pages. stopped() is asked while
    the page draws (every POLL_MS) and before printing; true raises ExportStopped."""
    report = progress or (lambda p: None)
    stop = stopped or (lambda: False)
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright

    if kind not in PAGES:
        raise ExportError(f"Unknown kind of set: {kind}.")
    token = secrets.token_urlsafe(32)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _handler(ctx, token, repo))
    httpd.daemon_threads = True
    origin = f"http://127.0.0.1:{httpd.server_address[1]}"
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(args=["--disable-dev-shm-usage"])
            try:
                context = browser.new_context(viewport={"width": 1440, "height": 900},
                                              extra_http_headers={TOKEN_HEADER: token}, service_workers="block")
                # Only the print server: nothing the page asks for goes anywhere else.
                context.route("**/*", lambda route: route.continue_()
                              if route.request.url.startswith(origin + "/") else route.abort())
                page = context.new_page()
                page.set_default_timeout(READY_TIMEOUT_MS)   # nothing below may wait forever
                page.on("pageerror", lambda e: log.warning("pdf: page error: %s", e))
                crashed = []
                page.on("crash", lambda _: crashed.append(True))
                report({"phase": "drawing"})
                page.goto(f"{origin}/{PAGES[kind]}?set={quote(set_id)}&print=1")
                state = _wait_ready(page, report, crashed, PlaywrightError, stop)
                if state.get("state") != "ready":
                    raise ExportError(str(state.get("message") or "The pages could not be drawn."))
                if stop():
                    raise ExportStopped("Stopped.")
                report({"phase": "printing"})
                page.pdf(path=str(out), scale=SCALE, print_background=True, prefer_css_page_size=True)
                return int(state.get("pages") or 0)
            finally:
                browser.close()
    finally:
        httpd.shutdown()
        httpd.server_close()


def _wait_ready(page: Any, report: "Callable[[dict], None]", crashed: list, error: type,
                stop: "Callable[[], bool]") -> dict:
    """Ask the page how it goes every POLL_MS until it is ready or failed, passing each new
    progress on; a crashed page or one past READY_TIMEOUT_MS is an ExportError."""
    deadline = time.monotonic() + READY_TIMEOUT_MS / 1000
    last = None
    while True:
        if stop():
            raise ExportStopped("Stopped.")
        try:
            state = page.evaluate("() => window.__artaPrint || { state: 'loading', phase: 'drawing' }")
        except error:
            if crashed or page.is_closed():
                raise ExportError("The set is too large to print in one go.") from None
            raise
        if state.get("state") != "loading":
            return state
        now = {k: state[k] for k in ("phase", "done", "total") if k in state}
        if now != last:
            report(now)
            last = now
        if time.monotonic() > deadline:
            raise ExportError("The pages took too long to draw.")
        page.wait_for_timeout(POLL_MS)
