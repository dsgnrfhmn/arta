"""Parse the artist's master CV (a Google Doc, exported as OpenDocument text) into import entries.

The Doc has no real headings and no tables, only styled paragraphs and lists, so the
shape is read from the text itself:
  - A paragraph in capitals (JANE ROE, BIO, EDUCATION, PRESS (SELECTION)) opens a
    section and becomes a heading block.
  - Anything before the first section (the document title) is not CV content. It is
    skipped and reported.
  - A paragraph that is only a year or a year range (2024, 2015-2018) sets the year for the
    entries under it. Every following paragraph or list item is one text block,
    "YEAR<TAB>text", the same shape scripts/build_cv_master.py makes.
  - An entry that starts with its own year ("2024, Funding of ...") is one block with that
    year, whatever the year paragraph above it said.
  - A paragraph or item with no year in force (the name block, BIO, the collections) is a
    text block as it stands.
  - A lone short line with no digit, "|" or quote, directly followed by a year paragraph
    ("Selected talks, symposiums, workshops"), is a sub-heading: a text block that ends the
    year in force instead of inheriting it.
  - Empty paragraphs are layout and carry nothing. Zero-width characters and line
    separators are cleaned out of the text.

Everything the parser leaves out is returned in `skipped`, so a change in the file's
shape is visible instead of silently dropping content. The file is user content and
treated as hostile: size-capped, no DOCTYPE or entity declarations, text only.
"""

from __future__ import annotations

import io
import re
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass

from pipeline.cv_blocks import CvImportError, Entry

CONTENT_MAX = 5_000_000
_T = "{urn:oasis:names:tc:opendocument:xmlns:text:1.0}"
_OFFICE = "{urn:oasis:names:tc:opendocument:xmlns:office:1.0}"
_YEAR = r"\d{4}(?:\s*[-–]\s*\d{4})?"
_YEAR_ONLY = re.compile(_YEAR)
_YEAR_LEAD = re.compile(rf"({_YEAR})\s*,\s*(.+)", re.S)
_QUOTES = "|\u201c\u201d\u201e\"\u00ab\u00bb"
_INVISIBLE = dict.fromkeys(map(ord, "​‌‍⁠﻿"))


@dataclass(frozen=True)
class ParsedCv:
    items: list[tuple[str, Entry]]   # (section, entry) in document order
    skipped: list[str]               # what was left out, and why


def _text(e: ET.Element) -> str:
    out = e.text or ""
    for c in e:
        if c.tag == _T + "line-break":
            out += " "
        elif c.tag == _T + "tab":
            out += " "
        elif c.tag == _T + "s":
            out += " " * int(c.get(_T + "c", "1"))
        elif c.tag == _T + "soft-page-break":
            pass
        else:
            out += _text(c)
        out += c.tail or ""
    return out


def _clean(s: str) -> str:
    return " ".join(s.translate(_INVISIBLE).split())


def _is_heading(text: str) -> bool:
    return any(ch.isalpha() for ch in text) and text == text.upper()


def _is_subheading(text: str, next_text: str | None) -> bool:
    return (
        next_text is not None
        and _YEAR_ONLY.fullmatch(next_text) is not None
        and len(text.split()) <= 8
        and not any(ch.isdigit() or ch in _QUOTES for ch in text)
    )


def _content(data: bytes) -> bytes:
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
        info = z.getinfo("content.xml")
    except (zipfile.BadZipFile, KeyError):
        raise CvImportError("The master CV is not an OpenDocument text file.") from None
    if info.file_size > CONTENT_MAX:
        raise CvImportError("The master CV is larger than expected.")
    xml = z.read("content.xml")
    if b"<!DOCTYPE" in xml or b"<!ENTITY" in xml:
        raise CvImportError("The master CV has a DOCTYPE or entity declaration; refusing it.")
    return xml


def _paragraphs(body: ET.Element):
    """Every paragraph in document order, with whether it sits in a list."""
    for el in body:
        if el.tag == _T + "p":
            yield el, False
        elif el.tag == _T + "list":
            for p in el.iter(_T + "p"):
                yield p, True


def parse(data: bytes) -> ParsedCv:
    try:
        root = ET.fromstring(_content(data))
    except ET.ParseError:
        raise CvImportError("The master CV's content cannot be parsed.") from None
    body = root.find(f".//{_OFFICE}text")
    if body is None:
        raise CvImportError("The master CV has no text body.")

    items: list[tuple[str, Entry]] = []
    skipped: list[str] = []
    section = ""
    year = ""

    paras = [(_clean(_text(p)), in_list) for p, in_list in _paragraphs(body)]
    paras = [(t, in_list) for t, in_list in paras if t]
    for n, (text, in_list) in enumerate(paras):
        if not in_list and _is_heading(text):
            section, year = text, ""
            items.append((section, Entry("heading", text)))
            continue
        if not section:
            skipped.append(f"loose text {text[:50]!r} before the first section")
            continue
        if not in_list and _YEAR_ONLY.fullmatch(text):
            year = text
            continue
        nxt = paras[n + 1] if n + 1 < len(paras) else None
        if not in_list and _is_subheading(text, nxt[0] if nxt and not nxt[1] else None):
            year = ""
            items.append((section, Entry("text", text)))
            continue
        lead = _YEAR_LEAD.fullmatch(text)
        if lead:
            items.append((section, Entry("text", f"{lead.group(1)}\t{lead.group(2)}")))
        elif year:
            items.append((section, Entry("text", f"{year}\t{text}")))
        else:
            items.append((section, Entry("text", text)))
    if not items:
        raise CvImportError("The master CV has no content.")
    return ParsedCv(items, skipped)
