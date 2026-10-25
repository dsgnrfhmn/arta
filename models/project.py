"""A project as the Portfolio page sees it: images plus text variants.

Nothing here knows where the project lives (Drive or the fixture tree). The
folder is messy by nature, so every part is optional: a project may have no
text, one language, no images at all. Files that are not usable images or text
are kept in `unsupported`, so nothing disappears silently.

Names and text are untrusted input. The sources pass them through
core.sanitize before building these objects; consumers render them with
textContent, never as HTML.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Image kinds the sources can identify by content. The thumbnail cache decides
# which of them it can decode.
IMAGE_KINDS = ("jpeg", "png", "webp", "heic")
LANGUAGES = ("en", "uk")


@dataclass(frozen=True)
class ImageRef:
    id: str           # opaque, derived from the path; the only handle clients use
    name: str         # sanitized file name
    group: str | None  # None: project root; else the subfolder path below the project
    kind: str         # one of IMAGE_KINDS, from the file's content, not its name
    size: int
    # From the file name and folders (sources/image_meta.py), same for every source.
    role: str = "work"        # "work" | "exhibition": which picker row it belongs to
    author: str = "not-specified"
    place: str = ""
    year: str = ""
    # Pixels as the picture is shown; 0 when unknown (the sheet layout then assumes landscape).
    width: int = 0
    height: int = 0

    def to_api(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "group": self.group, "kind": self.kind, "size": self.size,
                "role": self.role, "author": self.author, "place": self.place, "year": self.year,
                "width": self.width, "height": self.height}


@dataclass(frozen=True)
class UnsupportedFile:
    name: str
    group: str | None
    size: int
    reason: str

    def to_api(self) -> dict[str, Any]:
        return {"name": self.name, "group": self.group, "size": self.size, "reason": self.reason}


@dataclass(frozen=True)
class Project:
    id: str
    name: str
    images: tuple[ImageRef, ...] = ()
    descriptions: dict[str, str] = field(default_factory=dict)  # language -> text
    unsupported: tuple[UnsupportedFile, ...] = ()

    def summary_api(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "image_count": len(self.images),
            "languages": sorted(self.descriptions),
        }

    def to_api(self) -> dict[str, Any]:
        return {
            **self.summary_api(),
            "images": [i.to_api() for i in self.images],
            "descriptions": dict(self.descriptions),
            "unsupported": [u.to_api() for u in self.unsupported],
        }
