"""Sanitize user-editable text before it is stored.

Plain text only. Markup is stripped (script and style content is dropped with
its tags), entities are decoded, control characters are removed, and an
over-long value is rejected rather than silently cut.

Stored text is data: every consumer must render it with textContent (or an
equivalent), never as HTML. This module does not make a value safe to inject
into markup.

ANSI escape sequences and the zero-width / bidi characters are removed as in
job-agent's core/sanitize.py (job-agent commit be72bf2), ported with the same
character lists. The ANSI pass has to run first: dropping the ESC control
character alone would leave "[31m" behind in the text.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

# ANSI escape sequences (colour codes, cursor movement, etc.).
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

# Zero-width and bidi-override characters used to hide or reorder text.
_INVISIBLE = re.compile(
    "[\u200b\u200c\u200d\ufeff"   # zero-width space/non-joiner/joiner, BOM
    "\u202a\u202b\u202c\u202d\u202e"  # bidi embedding/override
    "\u2066\u2067\u2068\u2069]"  # bidi isolate
)

_SKIP = {"script", "style"}


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP:
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in _SKIP and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def plain_text(value: object, max_len: int) -> str:
    if not isinstance(value, str):
        raise ValueError("Expected text.")
    value = _INVISIBLE.sub("", _ANSI_ESCAPE.sub("", value))
    p = _Text()
    p.feed(value)
    p.close()
    text = "".join(p.parts).replace("\r\n", "\n").replace("\r", "\n")
    text = "".join(ch for ch in text if ch in "\n\t" or ch.isprintable())
    text = text.strip()
    if len(text) > max_len:
        raise ValueError(f"Text is longer than {max_len} characters.")
    return text
