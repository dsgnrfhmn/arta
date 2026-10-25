"""IntakeSource over artconnect.com's public opportunities listing.

Read-only and rate-limited (sources/web_listing.py). Residencies and open calls
are kept (dsgnrfhmn, 2026-10-10); grants, awards, jobs and the rest are skipped.

Layout robustness (dsgnrfhmn, 2026-10-10, "A + drift"): every step is a chain from the
most stable signal to the least, no model involved.

  listing   1. title link `/opportunity/<id>` + type link `?types=<TYPE>` in the card
            2. any link to `/opportunity/<id>` (URL pattern only); the type is then
               read from the detail page
  detail    1. schema.org `Event` in a JSON-LD block (name, description, endDate)
            2. any JSON-LD object with a name and a description
            3. <meta> / OpenGraph, <h1>, <title>, <time datetime>

Using anything but the first of a chain is drift: it is reported in `notes` (shown
in the intake report), logged, and one sample page per kind is written to
`debug_dir` (outside the repo, mode 0600) so the change can be looked at. When no
step works, the run fails with IntakeError rather than storing guesses.
"""

from __future__ import annotations

import html
import json
import logging
import os
import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable

from sources.intake_source import IncomingOpportunity, IntakeError
from sources.web_listing import WebListingSource, html_to_text

log = logging.getLogger("arta.artconnect")

KEPT = {"ART_RESIDENCY": "Residency", "OPEN_CALL": "Open call"}
_ID = r"[A-Za-z0-9_-]{6,40}"
_TITLE = re.compile(rf'href="/opportunity/({_ID})">([^<]+)</a>')
_ANY_LINK = re.compile(rf'<a\b[^>]*?href="(?:https://www\.artconnect\.com)?/opportunity/({_ID})"[^>]*>(.*?)</a>', re.S)
_TYPE = re.compile(r'href="/opportunities\?[^"]*types=([A-Z_]+)')
_LD = re.compile(r'<script type="application/ld\+json"[^>]*>(.*?)</script>', re.S)


class _Meta(HTMLParser):
    """<meta> content, the first <h1>, <title> and <time datetime>."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self.h1 = self.title = self.time = ""
        self._in: str | None = None

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "meta":
            key = (a.get("property") or a.get("name") or "").lower()
            if key and "content" in a:
                self.meta.setdefault(key, a["content"])
        elif tag == "time" and not self.time and a.get("datetime"):
            self.time = a["datetime"]
        elif tag in ("h1", "title") and not getattr(self, tag):
            self._in = tag

    def handle_endtag(self, tag):
        if tag == self._in:
            self._in = None

    def handle_data(self, data):
        if self._in:
            setattr(self, self._in, getattr(self, self._in) + data)


def _ld_objects(page_html: str) -> Iterable[dict]:
    for m in _LD.finditer(page_html):
        try:
            data = json.loads(m.group(1))
        except ValueError:
            continue
        stack = [data]
        while stack:
            d = stack.pop()
            if isinstance(d, list):
                stack.extend(d)
            elif isinstance(d, dict):
                yield d
                if isinstance(d.get("@graph"), list):
                    stack.extend(d["@graph"])


class ArtConnectSource(WebListingSource):
    name = "artconnect"
    host = "www.artconnect.com"

    def __init__(self, client=None, max_pages=None, debug_dir: Path | None = None) -> None:
        super().__init__(client=client, max_pages=max_pages)
        self.debug_dir = debug_dir
        self.notes: list[str] = []
        self._sampled: set[str] = set()

    # -- drift reporting ---------------------------------------------------
    def _drift(self, kind: str, message: str, page_html: str) -> None:
        if message not in self.notes:
            self.notes.append(message)
            log.warning("artconnect layout drift: %s", message)
        if self.debug_dir is not None and kind not in self._sampled:
            self._sampled.add(kind)
            try:
                self.debug_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
                fd = os.open(self.debug_dir / f"artconnect-{kind}.html", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(page_html)
            except OSError:
                log.warning("artconnect: could not save the %s sample page", kind)

    def fetch(self, seen):
        self.notes, self._sampled = [], set()
        return super().fetch(seen)

    # -- listing -----------------------------------------------------------
    def page_url(self, page: int) -> str:
        base = f"https://{self.host}/opportunities"
        return base if page == 1 else f"{base}?page={page}"

    @staticmethod
    def _typed(matches: list, types: list[tuple[int, str]], page_len: int) -> Iterable[tuple[str, str, str | None]]:
        seen: set[str] = set()
        for i, m in enumerate(matches):
            if m.group(1) in seen:
                continue
            seen.add(m.group(1))
            end = matches[i + 1].start() if i + 1 < len(matches) else page_len
            kind = next((t for pos, t in types if m.start() < pos < end), None)
            yield m.group(1), m.group(2), kind

    def parse_list(self, page_html: str) -> Iterable[dict]:
        types = [(m.start(), m.group(1)) for m in _TYPE.finditer(page_html)]
        primary = list(_TITLE.finditer(page_html))
        rows = [(i, html.unescape(t), k) for i, t, k in self._typed(primary, types, len(page_html)) if k]
        if rows:
            for id_, title, kind in rows:
                if kind in KEPT:
                    yield {"id": id_, "url": f"/opportunity/{id_}", "type": KEPT[kind], "title": title}
            return
        # Fallback: the card markup changed. Links to /opportunity/<id> alone.
        links = list(_ANY_LINK.finditer(page_html))
        if links:
            self._drift("listing", "listing: card layout changed, items found by URL pattern only", page_html)
        seen: set[str] = set()
        for m in links:
            id_ = m.group(1)
            title = html_to_text(m.group(2), 300)
            if id_ in seen or not title:
                continue
            seen.add(id_)
            yield {"id": id_, "url": f"/opportunity/{id_}", "type": None, "title": title}

    # -- detail ------------------------------------------------------------
    def parse_detail(self, page_html: str, entry: dict) -> IncomingOpportunity | None:
        kind = entry.get("type")
        if kind is None:
            m = _TYPE.search(page_html)
            code = m.group(1) if m else None
            if code is not None and code not in KEPT:
                return None
            kind = KEPT.get(code) or "unknown"
            if kind == "unknown":
                self._drift("type", "detail: item type not found, kept as unknown", page_html)

        name = description = deadline = ""
        found = None
        objs = list(_ld_objects(page_html))
        for o in objs:
            t = o.get("@type")
            t = t if isinstance(t, list) else [t]
            if "Event" in t and isinstance(o.get("name"), str):
                found = ("event", o)
                break
        if found is None:
            found = next((("jsonld", o) for o in objs if isinstance(o.get("name"), str) and isinstance(o.get("description"), str)), None)
        if found:
            o = found[1]
            name, description = o.get("name", ""), o.get("description", "") if isinstance(o.get("description"), str) else ""
            deadline = o.get("endDate") if isinstance(o.get("endDate"), str) else ""
            if found[0] != "event":
                self._drift("detail", "detail: no Event block, used another JSON-LD object", page_html)
        else:
            meta = _Meta()
            meta.feed(page_html)
            name = meta.meta.get("og:title") or meta.h1 or meta.title
            description = meta.meta.get("og:description") or meta.meta.get("description") or ""
            deadline = meta.time
            if not name:
                self._drift("detail-failed", f"detail: item {entry['id']} unreadable, no known structure", page_html)
                raise IntakeError(f"{self.host} item {entry['id']} has no readable structure; the page layout may have changed.")
            self._drift("detail", "detail: no JSON-LD, used meta tags (description may be shortened)", page_html)

        text = lambda v: html_to_text(v if isinstance(v, str) else "", 100_000)  # noqa: E731
        name = text(name) or entry["title"]
        description = text(description).replace("**", "")
        if not deadline:
            self._drift("deadline", "detail: no deadline found on a page", page_html)
        body = "\n".join(filter(None, [f"Type: {kind}", f"Deadline: {deadline}" if deadline else "", description]))
        return IncomingOpportunity(
            message_id=entry["id"], sender="ArtConnect", subject=name,
            received_at="", body=body, link=entry["url"], description=description, deadline=deadline,
        )
