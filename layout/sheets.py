"""Which sheet template each picture and text of a portfolio entry gets.

The templates are the page types of the artist's own portfolio PDF (dsgnrfhmn, 2026-10-24;
it supersedes Figma section 328:19053 for the preview; the geometry of each template
is in dis-system's portfolio-sheet component).
Every sheet is 842x599. One entry (a project with its picked pictures and its text)
becomes a list of sheets:

  opening (one, always):
    band         no text: title and year on top, the first work across the full
                 width below it (PDF p2, p18, p22, p30)
    grid-side    4 works or more: the text column left, the first four works in a
                 2 x 2 grid right, no captions (p3, p19)
    side         first work tall or square: the text column left, the picture right
                 at full height (p5, p13, p15, p21, p24, p28, p31)
    figure-side  first work wide: the text column left, the picture right, running
                 off the right edge, caption under it (p6, p8)
  then the works not on the opening, in pick order:
    grid         four at a time while four or more are left, a 2 x 2 grid
    full         a wide one: full bleed, caption over it (p4, p9, p14, p17, p26)
    figure       a tall or square one: centred on white, caption bottom right (p29)
  then the text that did not fit the opening column:
    text         one column per sheet
  then the exhibition photos, one per sheet, full or figure as above.

A picture's shape comes from ImageRef.width/height; unknown (0) counts as wide.
Pictures are cropped where the PDF crops them (band, side, figure-side, the grids,
full: the box is filled) and whole where it shows them whole (figure); the
template says which, the renderer (dis-system portfolio-sheet) draws it.

Arrangement and drawing are separate. `arrange` alone decides which picture sits
in which cell of a grid (grid-side, grid). A saved manual placement (not built yet)
overrides that one function's result and nothing else changes: every grid sheet
carries its address, the entry id, `grid_page` (0-based within the entry) and each
slot's `cell`.

Sheet shape (what the API returns, what a renderer draws):

    {"layout", "entry_id", "project_id", "title", "subtitle",
     "images": [{"id", "role": "work" | "exhibition", "caption": {"prefix", "label", "suffix", "lines"}}],
     "paragraphs": [str],
     "grid": {"cols", "rows"} | None, "grid_page": int | None,
     "slots": [{"cell": int, "image": {...} | None}] | None}

`images` is the filled cells in cell order; `slots` (the grids only) has every cell,
empty ones with `image: None`.

Text and names are untrusted; renderers use textContent, never HTML.
The character budgets are estimates, not measurements; like every page count they
are mock until the real pagination engine exists.
"""

from __future__ import annotations

from typing import Any

from models.project import ImageRef, Project
from sources.image_meta import NOT_SPECIFIED, title_of

GRID_COLS, GRID_ROWS = 2, 2          # grid-side and grid
# The layouts a set may choose per sheet (dsgnrfhmn, 2026-10-24: a vertical menu on the
# sheet; the choice belongs to that set only). Each takes this many works.
OPENING_LAYOUTS = {"grid-side": 4, "side": 1, "figure-side": 1, "band": 1}
WORK_LAYOUTS = {"full": 1, "figure": 1, "pair": 2, "triple": 3, "grid": 4}
EXHIBITION = "Exhibition view, "     # caption prefix of an exhibition view
TEXT_ON_OPENING = 1000  # characters of 12px text left under the title, about 20 lines of 320px
TEXT_PER_SHEET = 1600   # characters in a whole text column, about 33 lines
TALL = 0.9              # height/width from which a picture counts as tall (square included)


def _caption(image: ImageRef, project: Project, prefix: str = "") -> dict[str, Any]:
    # Place and author on one line, as the PDF has them: "Art House, Eastmoor (YY) © Jane Roe".
    line = " ".join(part for part in (image.place, f"© {image.author}" if image.author and image.author != NOT_SPECIFIED else "") if part)
    lines = [line] if line else []
    return {
        "prefix": prefix,
        "label": title_of(image.name) or project.name,
        "suffix": f", {image.year}" if image.year else "",
        "lines": lines,
    }


def _image(image: ImageRef, project: Project, prefix: str = "") -> dict[str, Any]:
    # role: a work is drawn whole, never cropped; an exhibition view may fill its cell
    # (dsgnrfhmn, 2026-10-24).
    return {"id": image.id, "role": "exhibition" if image.role == "exhibition" else "work",
            "caption": _caption(image, project, prefix)}


def paragraphs(text: str, limit: int = TEXT_PER_SHEET) -> list[list[str]]:
    """The text as groups of paragraphs, each group fitting one sheet."""
    pieces: list[str] = []
    for block in (b.strip() for b in text.replace("\r\n", "\n").split("\n\n")):
        while len(block) > limit:
            cut = block.rfind(" ", 0, limit)
            cut = cut if cut > 0 else limit
            pieces.append(block[:cut].strip())
            block = block[cut:].strip()
        if block:
            pieces.append(block)
    groups: list[list[str]] = []
    used = 0
    for piece in pieces:
        if not groups or used + len(piece) > limit:
            groups.append([])
            used = 0
        groups[-1].append(piece)
        used += len(piece)
    return groups


def description(project: Project) -> str:
    """One text per language exists (short, middle and full are not separate
    files yet): English if there is one, else the first language."""
    descriptions = project.descriptions
    return descriptions.get("en") or descriptions[sorted(descriptions)[0]] if descriptions else ""


def _sheet(layout: str, entry: dict[str, Any], **fields: Any) -> dict[str, Any]:
    return {"layout": layout, "entry_id": entry["id"], "project_id": entry["project_id"],
            "title": "", "subtitle": "", "images": [], "paragraphs": [],
            "grid": None, "grid_page": None, "slots": None, **fields}


def arrange(images: list[ImageRef], cols: int, rows: int) -> list[list[ImageRef | None]]:
    """Grid pages of cols x rows cells, filled in pick order, left to right and top to
    bottom, the last page padded with None (an empty cell). The one place that says which
    picture sits in which cell; a saved manual placement would override it here."""
    cells = cols * rows
    pages = []
    for start in range(0, len(images), cells):
        chunk: list[ImageRef | None] = list(images[start:start + cells])
        pages.append(chunk + [None] * (cells - len(chunk)))
    return pages


def tall(image: ImageRef) -> bool:
    return bool(image.width and image.height) and image.height >= TALL * image.width


def _grid_sheet(layout: str, entry: dict[str, Any], project: Project, cells: list[ImageRef | None], page: int,
                cols: int, rows: int, prefix: str = "", **fields: Any) -> dict[str, Any]:
    slots = [{"cell": n, "image": _image(i, project, prefix) if i else None} for n, i in enumerate(cells)]
    return _sheet(layout, entry, grid={"cols": cols, "rows": rows}, grid_page=page, slots=slots,
                  images=[s["image"] for s in slots if s["image"]], **fields)


def entry_sheets(entry: dict[str, Any], project: Project | None) -> list[dict[str, Any]]:
    """The entry's sheets. `entry["layout_plan"]` (optional, the set's own choice per
    sheet) names the opening's layout and then one layout per works sheet, in order;
    where it ends, or names nothing valid, the automatic choice below takes over.
    Every sheet of works carries `plan_index` (0 for the opening), the position the
    page's layout menu addresses."""
    if project is None:
        return [_sheet("missing", entry)]
    by_id = {i.id: i for i in project.images}
    works = [by_id[i] for i in entry["works"] if i in by_id]
    exhibitions = [by_id[i] for i in entry["exhibitions"] if i in by_id]
    # A text_override that is present wins, also empty: a page deleted from the set took
    # its text with it (dsgnrfhmn, 2026-10-24); no override means the project's own text.
    text = entry["text_override"] if isinstance(entry.get("text_override"), str) else description(project)
    first = works[0] if works else None
    # The set's own edits of the texts (dsgnrfhmn, 2026-10-24) win over the project's.
    head = {"title": entry.get("title_override") or project.name,
            "subtitle": entry.get("subtitle_override") or (first.year if first else "")}
    plan = list(entry.get("layout_plan") or [])
    chosen = plan[0] if plan and plan[0] in OPENING_LAYOUTS else None
    if not works and entry.get("text_override") == "":
        # The set deleted the opening's pictures and text (dsgnrfhmn, 2026-10-24): no
        # title-only page is left; the exhibition views, if any, follow at once.
        return _with_captions(entry, _exhibition_sheets(entry, project, exhibitions, 0))

    if chosen == "band" or (chosen is None and not text.strip() and not (first and tall(first)) and len(works) < 4):
        opening, used = "band", works[:1]
        rest_text = paragraphs(text) if text.strip() else []      # no text column on a band
    else:
        groups = paragraphs(text, TEXT_ON_OPENING)
        on_opening = groups[0] if groups else []
        rest_text = paragraphs(" \n\n".join(p for g in groups[1:] for p in g)) if len(groups) > 1 else []
        if chosen:
            opening = chosen
        elif len(works) >= 4:
            opening = "grid-side"
        elif first and tall(first):
            opening = "side"
        else:
            opening = "figure-side"
        used = works[:OPENING_LAYOUTS[opening]]
        head["paragraphs"] = on_opening
    grid_page = 0
    if opening == "grid-side":
        cells = arrange(used, GRID_COLS, GRID_ROWS)[0] if used else [None] * (GRID_COLS * GRID_ROWS)
        sheets = [_grid_sheet("grid-side", entry, project, cells, grid_page, GRID_COLS, GRID_ROWS, plan_index=0, **head)]
        grid_page += 1
    else:
        sheets = [_sheet(opening, entry, images=[_image(i, project) for i in used], plan_index=0, **head)]
    sheets[0]["entry_text"] = text      # the whole text, for editing it in place

    rest = works[len(used):]
    index = 1
    for layout in plan[1:]:
        if not rest:
            break
        if layout not in WORK_LAYOUTS:
            continue
        n = WORK_LAYOUTS[layout]
        take, rest = rest[:n], rest[n:]
        if layout == "grid":
            sheets.append(_grid_sheet("grid", entry, project, arrange(take, GRID_COLS, GRID_ROWS)[0], grid_page,
                                      GRID_COLS, GRID_ROWS, plan_index=index))
            grid_page += 1
        elif layout in ("pair", "triple"):
            sheets.append(_grid_sheet(layout, entry, project, take + [None] * (n - len(take)), grid_page, n, 1,
                                      plan_index=index))
            grid_page += 1
        else:
            sheets.append(_sheet(layout, entry, images=[_image(take[0], project)], plan_index=index))
        index += 1
    for cells in arrange(rest[:len(rest) - len(rest) % 4], GRID_COLS, GRID_ROWS):
        sheets.append(_grid_sheet("grid", entry, project, cells, grid_page, GRID_COLS, GRID_ROWS, plan_index=index))
        grid_page += 1
        index += 1
    for image in rest[len(rest) - len(rest) % 4:]:
        sheets.append({**_single(entry, project, image), "plan_index": index})
        index += 1
    for group in rest_text:
        sheets.append(_sheet("text", entry, paragraphs=group))
    return _with_captions(entry, sheets + _exhibition_sheets(entry, project, exhibitions, grid_page))


def _exhibition_sheets(entry: dict[str, Any], project: Project, exhibitions: list[ImageRef],
                       grid_page: int) -> list[dict[str, Any]]:
    sheets: list[dict[str, Any]] = []
    # Exhibition views: the set's own plan for them (entry["exhibition_plan"], one
    # layout per sheet from WORK_LAYOUTS, dsgnrfhmn 2026-10-24), then one per sheet by shape.
    # Each carries `exhibition_index`, the position the page's layout menu addresses.
    rest_ex, index = list(exhibitions), 0
    for layout in entry.get("exhibition_plan") or []:
        if not rest_ex:
            break
        if layout not in WORK_LAYOUTS:
            continue
        n = WORK_LAYOUTS[layout]
        take, rest_ex = rest_ex[:n], rest_ex[n:]
        if layout == "grid":
            sheet = _grid_sheet("grid", entry, project, arrange(take, GRID_COLS, GRID_ROWS)[0], grid_page,
                                GRID_COLS, GRID_ROWS, EXHIBITION)
            grid_page += 1
        elif layout in ("pair", "triple"):
            sheet = _grid_sheet(layout, entry, project, take + [None] * (n - len(take)), grid_page, n, 1, EXHIBITION)
            grid_page += 1
        else:
            sheet = _sheet(layout, entry, images=[_image(take[0], project, EXHIBITION)])
        sheets.append({**sheet, "exhibition_index": index})
        index += 1
    for image in rest_ex:
        sheets.append({**_single(entry, project, image, EXHIBITION), "exhibition_index": index})
        index += 1
    return sheets


def _with_captions(entry: dict[str, Any], sheets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A caption the set has edited: its first line is the label, the rest its lines."""
    captions = entry.get("caption_overrides") or {}
    for sheet in sheets:
        for image in sheet["images"]:
            text = captions.get(image["id"])
            if text:
                first_line, *rest = text.split("\n")
                image["caption"] = {"prefix": "", "label": first_line, "suffix": "", "lines": rest, "edited": True}
    return sheets


def _single(entry: dict[str, Any], project: Project, image: ImageRef, prefix: str = "") -> dict[str, Any]:
    """One picture on its own sheet: a wide one full bleed, a tall one as a figure."""
    return _sheet("figure" if tall(image) else "full", entry, images=[_image(image, project, prefix)])
