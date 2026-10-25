"""Helpers shared by ProjectSource implementations: what a file is, judged by
its content, and text out of a docx. Stdlib only.

The real project folder does not follow its names: an extension-less 31 MB
file can be audio, a `.jpg_original` can be a JPEG, `.heic` needs its own
decoder. So type comes from the first bytes, never from the name.
"""

from __future__ import annotations

import re
import struct
import zipfile
from xml.etree import ElementTree

MAX_DOCX_XML = 5 * 1024 * 1024
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_HEIC_BRANDS = {b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"mif1", b"msf1"}


def sniff(head: bytes) -> tuple[str | None, str]:
    """(image kind or None, reason it is not a supported image)."""
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg", ""
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png", ""
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp", ""
    if head[4:8] == b"ftyp" and head[8:12] in _HEIC_BRANDS:
        return "heic", ""
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return None, "audio (WAV), not an image"
    if head.startswith(b"PK\x03\x04"):
        return None, "archive or document, not an image"
    return None, "not a supported image type"


# How much of a file is read to find its size. Headers sit at the start; a JPEG
# can carry a large embedded thumbnail or profile before its frame header.
_SIZE_READ = 1024 * 1024


def _jpeg_size(data: bytes) -> tuple[int, int]:
    i, n, orientation = 2, len(data), 1
    while i + 4 <= n:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker == 0xFF:
            i += 1
            continue
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        length = struct.unpack(">H", data[i + 2:i + 4])[0]
        if marker == 0xE1 and data[i + 4:i + 10] == b"Exif\0\0":
            orientation = _exif_orientation(data[i + 10:i + 2 + length])
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            height, width = struct.unpack(">HH", data[i + 5:i + 9])
            return (height, width) if orientation in (5, 6, 7, 8) else (width, height)
        i += 2 + length
    return 0, 0


def _exif_orientation(tiff: bytes) -> int:
    """The EXIF Orientation tag (1 when absent or unreadable)."""
    try:
        end = "<" if tiff[:2] == b"II" else ">"
        offset = struct.unpack(end + "I", tiff[4:8])[0]
        count = struct.unpack(end + "H", tiff[offset:offset + 2])[0]
        for k in range(count):
            entry = tiff[offset + 2 + 12 * k:offset + 14 + 12 * k]
            if struct.unpack(end + "H", entry[:2])[0] == 0x0112:
                return struct.unpack(end + "H", entry[8:10])[0]
    except (struct.error, IndexError):
        pass
    return 1


def _webp_size(data: bytes) -> tuple[int, int]:
    chunk = data[12:16]
    if chunk == b"VP8 ":
        w, h = struct.unpack("<HH", data[26:30])
        return w & 0x3FFF, h & 0x3FFF
    if chunk == b"VP8L":
        bits = struct.unpack("<I", data[21:25])[0]
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    if chunk == b"VP8X":
        return int.from_bytes(data[24:27], "little") + 1, int.from_bytes(data[27:30], "little") + 1
    return 0, 0


def _heic_size(data: bytes) -> tuple[int, int]:
    """The largest image-spatial-extents box ('ispe'), turned when a rotation box
    ('irot') asks for a quarter turn. HEIC phones store portrait as rotated."""
    sizes = [struct.unpack(">II", data[m + 8:m + 16]) for m in
             (x.start() for x in re.finditer(b"ispe", data)) if m + 16 <= len(data)]
    if not sizes:
        return 0, 0
    w, h = max(sizes, key=lambda s: s[0] * s[1])
    turned = any(data[m.start() + 4] & 1 for m in re.finditer(b"irot", data) if m.start() + 5 <= len(data))
    return (h, w) if turned else (w, h)


def image_size(path, kind: str) -> tuple[int, int]:
    """(width, height) of an image as it is shown (EXIF / HEIC rotation applied),
    from its header. (0, 0) when it cannot be read: callers treat that as unknown,
    never as an error, so one odd file does not hide a project."""
    try:
        with open(path, "rb") as f:
            data = f.read(_SIZE_READ)
        if kind == "png":
            w, h = struct.unpack(">II", data[16:24])
        elif kind == "jpeg":
            w, h = _jpeg_size(data)
        elif kind == "webp":
            w, h = _webp_size(data)
        elif kind == "heic":
            w, h = _heic_size(data)
        else:
            return 0, 0
    except (OSError, struct.error):
        return 0, 0
    return (w, h) if w > 0 and h > 0 else (0, 0)


def is_zip(head: bytes) -> bool:
    return head.startswith(b"PK\x03\x04")


def natural_key(name: str) -> tuple:
    """Sort 2 before 10, ignore case, and put 'a.jpg' before 'a1.jpg': the name
    without its extension decides first, the extension only breaks ties."""
    stem, dot, ext = name.rpartition(".") if "." in name.lstrip(".") else (name, "", "")
    parts = [int(p) if p.isdigit() else p.lower() for p in re.split(r"(\d+)", stem)]
    return (parts, ext.lower())


def docx_text(path) -> str:
    """Paragraph text of a .docx. Raises ValueError if it is not a readable docx."""
    try:
        with zipfile.ZipFile(path) as z:
            info = z.getinfo("word/document.xml")
            if info.file_size > MAX_DOCX_XML:
                raise ValueError("document is too large")
            root = ElementTree.fromstring(z.read(info))
    except (zipfile.BadZipFile, KeyError, ElementTree.ParseError) as e:
        raise ValueError("not a readable .docx") from e
    paragraphs = []
    for p in root.iter(f"{_W}p"):
        parts = []
        for node in p.iter():
            if node.tag == f"{_W}t" and node.text:
                parts.append(node.text)
            elif node.tag == f"{_W}tab":
                parts.append("\t")
            elif node.tag == f"{_W}br":
                parts.append("\n")
        paragraphs.append("".join(parts))
    return "\n".join(paragraphs)


def language_of(text: str) -> str:
    """'uk' when Cyrillic dominates the letters, else 'en'."""
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return "en"
    cyrillic = sum(1 for c in letters if "\u0400" <= c <= "\u04ff")
    return "uk" if cyrillic / len(letters) > 0.3 else "en"
