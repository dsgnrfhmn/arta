"""Draw the demo's project folders from demo/projects.json.

The result has the layout a real project folder has (sources/local_projects.py reads
it the same way): one folder per project, the works in its root with title, medium and
size in the file name, one subfolder per exhibition with the installation photos, and a
.docx description. Pictures come from demo/paint.py, so nothing binary is kept in the
repository and the same input always gives the same folder.
"""

from __future__ import annotations

import json
import shutil
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

from demo import paint

LONG_SIDE = 1400        # pixels of a work's longer side
JPEG_QUALITY = 86

_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
    '</Types>'
)
_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
    'Target="word/document.xml"/></Relationships>'
)


def pixels(size_cm: list[int]) -> tuple[int, int]:
    """The picture's pixel size: the work's proportions, LONG_SIDE on the longer side."""
    w, h = size_cm
    return (LONG_SIDE, round(LONG_SIDE * h / w)) if w >= h else (round(LONG_SIDE * w / h), LONG_SIDE)


def work_file(work: dict, artist: str) -> str:
    w, h = work["size"]
    return f'“{work["title"]}”, {work["medium"]}, {w}x{h} cm___{artist}.jpg'


def write_docx(path: Path, paragraphs: list[str]) -> None:
    """A minimal but valid .docx: one text paragraph per entry."""
    body = "".join(f"<w:p><w:r><w:t xml:space=\"preserve\">{escape(p)}</w:t></w:r></w:p>" for p in paragraphs)
    document = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                f"<w:body>{body}</w:body></w:document>")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in (("[Content_Types].xml", _CONTENT_TYPES), ("_rels/.rels", _RELS),
                           ("word/document.xml", document)):
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))   # fixed: same bytes every build
            z.writestr(info, data, zipfile.ZIP_DEFLATED)


def _save(img, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, "JPEG", quality=JPEG_QUALITY)


def build_projects(spec_path: Path, target: Path) -> int:
    """Draw every project of spec_path into target (which must not exist yet). Built in a
    sibling folder and renamed at the end, so a stopped build never looks finished.
    Returns the number of pictures drawn."""
    if target.exists():
        raise FileExistsError(f"{target} already exists.")
    spec = json.loads(spec_path.read_text("utf-8"))
    tmp = target.with_name(target.name + ".partial")
    shutil.rmtree(tmp, ignore_errors=True)
    drawn = 0
    for project in spec["projects"]:
        folder = tmp / project["name"]
        works = []
        for i, work in enumerate(project["works"]):
            img = paint.work(project["style"], project["seed"] + i, pixels(work["size"]))
            _save(img, folder / work_file(work, spec["artist"]))
            works.append(img)
            drawn += 1
        for e, show in enumerate(project["exhibitions"]):
            where = f'{show["photographer"]}__{show["venue"]}__{show["year"]}.jpg'
            seed = project["seed"] * 100 + e * 10
            for k, hung in enumerate(show["views"], 1):
                _save(paint.exhibition(seed + k, [works[i] for i in hung], frame=show["frame"]),
                      folder / show["folder"] / f"IW-{k}__{where}")
                drawn += 1
            for k, i in enumerate(show["details"], 1):
                _save(paint.detail(seed + 50 + k, works[i]), folder / show["folder"] / f"Detail-{k}__{where}")
                drawn += 1
        write_docx(folder / f'{project["name"]}.docx', project["description"])
    tmp.rename(target)
    return drawn
