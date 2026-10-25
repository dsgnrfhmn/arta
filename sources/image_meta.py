"""What a project image's file name and folder say: role, author, place, year.

One parser for every ProjectSource, so Drive and the fixture tree read the same
convention (the file name is the only carrier of metadata):

    [IW-|Detail-]Title-N__Author__Place__Year.ext

Author comes before place. Place and year are optional; the author is always
present, `not-specified` when nobody is known. A file that does not follow the
scheme still gets a role and `not-specified`; it is never dropped.

Role: an image is an exhibition photo when its name starts with `IW-` or
`Detail-`, or when any folder above it names an exposition, exhibition or
installation view. Everything else is a photo of the work.

Stdlib only. Callers sanitize the file name before parsing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

NOT_SPECIFIED = "not-specified"

_PREFIX = re.compile(r"^(IW|Detail)(?:[-_]|$)", re.I)
_EXHIBITION_FOLDER = re.compile(r"exposition|exhibition|installation view", re.I)
_YEAR = re.compile(r"^(?:19|20)\d\d$")
_GLUED_YEAR = re.compile(r"[-_ ]((?:19|20)\d\d)$")
_INDEX = re.compile(r"(?:[-.]\d+)+$")
_GLUED_PART = re.compile(r"_(?=[A-Z])")
_TYPED_EXT = re.compile(r"jpe?g$", re.I)
# "gallery One, Eastmoor (2022)", "Views__Academy (2021)", "2024, Exposition in Northbay"
_FOLDER_DATED = re.compile(r"^(?:[^_]+__)?(?P<p1>.+?)\s*\((?P<y1>(?:19|20)\d\d)\)$|^(?P<y2>(?:19|20)\d\d),\s*(?P<p2>.+)$")
_FOLDER_LEAD = re.compile(r"^(?:exposition|exhibition)\s+in\s+", re.I)


@dataclass(frozen=True)
class ImageMeta:
    role: str      # "work" | "exhibition"
    author: str    # display form; NOT_SPECIFIED when unknown
    place: str     # display form; "" when unknown
    year: str      # "" when unknown


@dataclass(frozen=True)
class Registry:
    """Canonical authors and places, with the spellings found in file names.

    Names on Drive are not renamed: fields come in any order, with typos and
    glued years (`IW-1__Project-Name__Gallery-Name__Author-Name-2026`).
    A token is an author or a place because the registry says so, not because
    of where it stands.
    """
    lookup: dict[str, tuple[str, str]]   # lower-case spelling -> (kind, canonical)

    @classmethod
    def from_dict(cls, data: dict) -> "Registry":
        lookup: dict[str, tuple[str, str]] = {}
        for kind, key in (("author", "authors"), ("place", "places")):
            for canon, variants in data.get(key, {}).items():
                for spelling in (canon, *variants):
                    lookup[spelling.lower()] = (kind, canon)
        return cls(lookup)

    def find(self, part: str) -> tuple[str, str] | None:
        key = part.lower()
        return self.lookup.get(key) or self.lookup.get(_INDEX.sub("", key))

    def author_at_end(self, head: str) -> str:
        """An author typed onto the title: `Detail-1-Author-Name`."""
        key = head.lower()
        for spelling, (kind, canon) in self.lookup.items():
            if kind == "author" and key.endswith("-" + spelling):
                return canon
        return ""


def load_registry(path) -> Registry:
    """profile/registry.json: {"authors": {"Jane-Roe": ["variant", ...]}, "places": {...}}.
    Raises ValueError on a file that is not that shape."""
    import json
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict) or not all(
            isinstance(data.get(k, {}), dict) and all(isinstance(v, list) for v in data.get(k, {}).values())
            for k in ("authors", "places")):
        raise ValueError(f"{path}: expected {{'authors': {{name: [variants]}}, 'places': {{...}}}}")
    return Registry.from_dict(data)


def _display(token: str) -> str:
    return token.replace("-", " ").replace("_", " ").strip()


def _by_registry(tokens: list[str], registry: Registry) -> tuple[str, str, str] | None:
    """(author, place, year) read by the registry, or None when no token is in it."""
    author = place = year = ""
    hit_any = False
    for token in tokens[1:]:
        for part in _GLUED_PART.split(token):
            if _YEAR.match(part):
                year = year or part
                continue
            glued = _GLUED_YEAR.search(part)
            if glued:
                year = year or glued.group(1)
                part = part[:glued.start()]
            hit = registry.find(part)
            if not hit:
                continue
            hit_any = True
            if hit[0] == "author":
                author = author or _display(hit[1])
            else:
                place = place or _display(hit[1])
    if not author:
        glued_author = registry.author_at_end(tokens[0])
        if glued_author:
            author, hit_any = _display(glued_author), True
    return (author, place, year) if hit_any else None


def _from_folders(folder_parts: tuple[str, ...], registry: Registry | None) -> tuple[str, str]:
    """(place, year) from the nearest dated folder: `gallery One, Eastmoor (2022)`."""
    for part in reversed(folder_parts):
        m = _FOLDER_DATED.match(part.strip())
        if m:
            place = _FOLDER_LEAD.sub("", m.group("p1") or m.group("p2")).strip()
            hit = registry.find(place.replace(" ", "-")) if registry else None
            return (_display(hit[1]) if hit and hit[0] == "place" else place), (m.group("y1") or m.group("y2"))
    return "", ""


def parse_image_meta(file_name: str, folder_parts: tuple[str, ...] = (),
                     registry: Registry | None = None) -> ImageMeta:
    stem = file_name.rpartition(".")[0] if "." in file_name.lstrip(".") else file_name
    if "__" in stem:
        stem = _TYPED_EXT.sub("", stem)
    tokens = [t for t in stem.split("__") if t]
    role = "exhibition" if _PREFIX.match(stem) or any(_EXHIBITION_FOLDER.search(p) for p in folder_parts) else "work"

    found = _by_registry(tokens, registry) if registry and tokens else None
    if found:
        author, place, year = found
    else:
        rest = tokens[1:]
        year = rest.pop() if rest and _YEAR.match(rest[-1]) else ""
        author = _display(rest[0]) if rest and rest[0].lower() != NOT_SPECIFIED else ""
        place = _display(rest[1]) if len(rest) > 1 else ""
    folder_place, folder_year = _from_folders(folder_parts, registry)
    return ImageMeta(role, author or NOT_SPECIFIED, place or folder_place, year or folder_year)


_TITLE_SUFFIX = re.compile(r"[-_ ]\d+(?:\.\d+)*$")
_UUID = re.compile(r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$", re.I)
_CAMERA_NAME = re.compile(r"^\d{6,}|^(?:IMG|DSC|DSCN|PXL|P)[-_ ]?\d|(?:^|[-_ ])IMG(?:[-_ ]|\d|$)", re.I)


def title_of(file_name: str) -> str:
    """The work or exhibition title a file name starts with: the first segment,
    without the IW-/Detail- prefix and the trailing counter. '' when nothing is
    left (`1.jpg`) or the name is a camera's (`IMG_5297`, `20230725_IMG_1`), or an exported id (a UUID)."""
    stem = file_name.rpartition(".")[0] if "." in file_name.lstrip(".") else file_name
    head = stem.split("__")[0]
    if _CAMERA_NAME.match(head) or _UUID.match(head):
        return ""
    head = _PREFIX.sub("", head, count=1)
    head = _TITLE_SUFFIX.sub("", head)
    title = _display(head)
    return "" if title.isdigit() else title


def caption(meta: ImageMeta) -> str:
    """'Jane Roe, Gallery One, 2023'; unknown parts stay out,
    except the author, which reads `not-specified`."""
    return ", ".join(p for p in (meta.author, meta.place, meta.year) if p)
