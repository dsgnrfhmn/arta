"""Shared base for intake sources that read a public website (ArtConnect and similar).

Everything here treats the site as hostile input, like an email body:

* `HttpClient` fetches https only, from an exact host allowlist, follows a redirect
  only to the same allowlisted host over https, caps the body at MAX_BYTES, accepts
  only text/html, and spaces requests at least `min_interval` seconds apart.
* `html_to_text` turns a page (or a fragment) into plain text. Unlike
  core/sanitize.plain_text alone it keeps block boundaries as newlines and drops
  everything a reader cannot see: script/style/template/svg/iframe, elements with
  `hidden`, aria-hidden or an inline display:none / visibility:hidden / font-size:0
  style. Hidden text is the usual carrier of prompt injection, so it never reaches
  the model. The result is still untrusted text: pipeline/intake and
  pipeline/prompt_guard treat it as data.

A concrete source subclasses `WebListingSource` and implements `parse_list` and
`parse_detail`; paging, the page cap, the seen-set and the polite fetching live here.
Links are stored as data and fetched only by this class, only on the allowlisted host.
"""

from __future__ import annotations

import logging
import re
import time
import urllib.error
import urllib.request
from html.parser import HTMLParser
from typing import Callable, Iterable
from urllib.parse import urljoin, urlsplit

from core import sanitize
from sources.intake_source import IncomingOpportunity, IntakeError

log = logging.getLogger("arta.web")

MAX_BYTES = 2_000_000
TIMEOUT = 20
USER_AGENT = "arta/1.0 (personal art-opportunity reader; contact: owner of this installation)"
TEXT_MAX = 100_000

_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
_DROP = {"script", "style", "noscript", "template", "svg", "iframe", "object", "embed", "head", "canvas", "math"}
_BLOCK = {
    "p", "div", "section", "article", "header", "footer", "main", "aside", "nav", "ul", "ol", "li", "table", "tr",
    "td", "th", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre", "br", "hr", "dl", "dt", "dd", "figure",
    "figcaption", "form", "fieldset", "details", "summary",
}
_HIDDEN_STYLE = re.compile(
    r"display\s*:\s*none|visibility\s*:\s*hidden|font-size\s*:\s*0(?:px|pt|em|rem|%)?\s*(?:;|$)|opacity\s*:\s*0\s*(?:;|$)",
    re.I,
)


class _Visible(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._stack: list[tuple[str, bool]] = []   # (tag, hides its content)
        self._hidden = 0

    @staticmethod
    def _hides(tag: str, attrs: list[tuple[str, str | None]]) -> bool:
        if tag in _DROP:
            return True
        d = {k.lower(): (v or "") for k, v in attrs}
        if "hidden" in d or d.get("aria-hidden", "").lower() == "true":
            return True
        return bool(_HIDDEN_STYLE.search(d.get("style", "")))

    def handle_starttag(self, tag, attrs):
        if tag in _VOID:
            if tag in _BLOCK and not self._hidden:
                self.parts.append("\n")
            return
        hides = self._hides(tag, attrs)
        self._stack.append((tag, hides))
        if hides:
            self._hidden += 1
        if tag in _BLOCK and not self._hidden:
            self.parts.append("\n")

    def handle_startendtag(self, tag, attrs):
        if tag in _BLOCK and not self._hidden:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in _VOID:
            return
        for i in range(len(self._stack) - 1, -1, -1):
            if self._stack[i][0] == tag:
                while len(self._stack) > i:
                    _, hides = self._stack.pop()
                    if hides:
                        self._hidden -= 1
                break
        if tag in _BLOCK and not self._hidden:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._hidden:
            self.parts.append(data)


def html_to_text(html: str, max_len: int = TEXT_MAX) -> str:
    """Visible text of an HTML document or fragment, as sanitized plain text."""
    if not isinstance(html, str):
        raise ValueError("Expected text.")
    p = _Visible()
    p.feed(html[: max_len * 4])
    p.close()
    raw = "".join(p.parts)
    lines = [re.sub(r"[ \t ]+", " ", ln).strip() for ln in raw.replace("\r", "\n").split("\n")]
    text = "\n".join(ln for ln in lines if ln)
    return sanitize.plain_text(text[:max_len], max_len)


def clean_link(url: str, base: str, host: str) -> str | None:
    """An absolute https URL on `host`, resolved against `base`, or None."""
    try:
        absolute = urljoin(base, url)
        parts = urlsplit(absolute)
        if parts.scheme != "https" or parts.hostname != host or parts.username or parts.password:
            return None
        if parts.port not in (None, 443) or any(ch.isspace() or ord(ch) < 0x20 for ch in absolute):
            return None
    except ValueError:
        return None
    return absolute


class _SameHostRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, host: str) -> None:
        self.host = host

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if clean_link(newurl, req.full_url, self.host) is None:
            raise urllib.error.URLError(f"redirect off {self.host} refused")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class HttpClient:
    """GET with the policy in the module docstring. `opener` and `sleep`/`clock`
    are injectable so tests never touch the network or wait."""

    def __init__(self, host: str, min_interval: float = 1.0, opener=None,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic) -> None:
        self.host = host
        self.min_interval = min_interval
        self._opener = opener or urllib.request.build_opener(_SameHostRedirect(host))
        self._sleep, self._clock = sleep, clock
        self._last: float | None = None

    def get(self, url: str) -> str:
        if clean_link(url, url, self.host) is None:
            raise IntakeError(f"Refused to fetch a URL outside https://{self.host}.")
        if self._last is not None:
            wait = self.min_interval - (self._clock() - self._last)
            if wait > 0:
                self._sleep(wait)
        self._last = self._clock()
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html"})
        try:
            with self._opener.open(req, timeout=TIMEOUT) as res:
                ctype = (res.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                if ctype != "text/html":
                    raise IntakeError(f"{self.host} answered with {ctype or 'no content type'}, expected text/html.")
                body = res.read(MAX_BYTES + 1)
        except IntakeError:
            raise
        except (urllib.error.URLError, OSError) as exc:
            raise IntakeError(f"Could not read {self.host}: {type(exc).__name__}.") from None
        if len(body) > MAX_BYTES:
            raise IntakeError(f"A page from {self.host} is larger than {MAX_BYTES} bytes.")
        return body.decode("utf-8", errors="replace")


class WebListingSource:
    """IntakeSource over a paged listing plus one detail page per item."""

    name = "web"
    host = ""
    list_url = ""        # first page; `{page}` is replaced for later pages
    max_pages = 7        # per run (dsgnrfhmn, 2026-10-10)
    max_items = 40       # detail pages per run; requests are 1 s apart, so a run stays near a minute.
                         # Items beyond it are not marked seen, so the next run takes them.

    def __init__(self, client: HttpClient | None = None, max_pages: int | None = None) -> None:
        self.client = client or HttpClient(self.host)
        if max_pages is not None:
            self.max_pages = max_pages

    # -- to implement ------------------------------------------------------
    def page_url(self, page: int) -> str:
        raise NotImplementedError

    def parse_list(self, html: str) -> Iterable[dict]:
        """Entries of one listing page: dicts with at least `id` and `url`, plus
        whatever the listing offers (`type`, ...). Entries to skip are not yielded."""
        raise NotImplementedError

    def parse_detail(self, html: str, entry: dict) -> IncomingOpportunity | None:
        """The item, or None when it is not one to keep. Raises IntakeError when unreadable."""
        raise NotImplementedError

    # -- shared -----------------------------------------------------------
    def fetch(self, seen: set[str]) -> list[IncomingOpportunity]:
        out: list[IncomingOpportunity] = []
        ids: set[str] = set()
        attempted = failed = 0
        for page in range(1, self.max_pages + 1):
            entries = list(self.parse_list(self.client.get(self.page_url(page))))
            if not entries:
                if page == 1:
                    raise IntakeError(f"{self.host} listing gave no items; the page layout may have changed.")
                break
            for e in entries:
                if e["id"] in seen or e["id"] in ids:
                    continue
                ids.add(e["id"])
                url = clean_link(e["url"], self.page_url(page), self.host)
                if url is None:
                    log.warning("web %s: skipped item %s with an off-host URL", self.name, e["id"])
                    continue
                if len(out) >= self.max_items:
                    return out
                attempted += 1
                try:
                    item = self.parse_detail(self.client.get(url), {**e, "url": url})
                    if item is not None:          # None: the item is not one this source keeps
                        out.append(item)
                except IntakeError as exc:
                    # One unreadable item must not stop the run, or let a hostile page
                    # block the intake; it stays unseen and is tried again next run.
                    log.warning("web %s: skipped item %s: %s", self.name, e["id"], exc)
                    failed += 1
        if attempted and failed == attempted:
            raise IntakeError(f"None of {attempted} items on {self.host} could be read; the page layout may have changed.")
        return out
