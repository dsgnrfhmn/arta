"""Build a master CV file (the LocalCv shape) from a plain-text transcription.

  python3 scripts/build_cv_master.py tests/fixtures/cv/figma_cv.txt tests/fixtures/cv/figma_master.json

The text file is the editable master.
If the output file exists, its blocks are the "existing" side of the merge in
pipeline/cv_blocks.py: same text keeps its id, new text gets a new id, and blocks
that disappeared are listed, not silently dropped. Nothing is written when the
input does not parse.

Text format (lines starting with // are comments):
  @short: SECTION; SECTION      referent -> the sections it contains (full = all)
  # TITLE                       a heading block, and the start of a section
  YEAR<TAB>text                 a text block
  ~<TAB>text                    a text block without a year
  +<TAB>text                    another line of the previous text block
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline.cv_blocks import CvImportError, Entry, import_blocks  # noqa: E402
from sources.cv_source import Block  # noqa: E402

REFERENT_NAMES = {"short": "Short CV", "middle": "Middle CV", "full": "Full CV"}


class ParseError(ValueError):
    pass


def parse(text: str) -> tuple[list[tuple[str, Entry]], dict[str, list[str]]]:
    """Returns [(section, entry)] in order, and {referent: [sections]}."""
    items: list[tuple[str, Entry]] = []
    referents: dict[str, list[str]] = {}
    section = ""
    for n, raw in enumerate(text.split("\n"), 1):
        if not raw.strip() or raw.startswith("//"):
            continue
        if raw.startswith("@"):
            name, _, rest = raw[1:].partition(":")
            if name not in ("short", "middle") or not rest.strip():
                raise ParseError(f"line {n}: bad referent line.")
            referents[name] = [s.strip() for s in rest.split(";") if s.strip()]
        elif raw.startswith("# "):
            section = raw[2:].strip()
            items.append((section, Entry("heading", section)))
        elif raw.startswith("+\t"):
            if not items or items[-1][1].kind != "text":
                raise ParseError(f"line {n}: a continuation line needs a text block above it.")
            sec, prev = items[-1]
            items[-1] = (sec, Entry("text", prev.text + "\n" + raw[2:].strip()))
        elif "\t" in raw:
            year, _, body = raw.partition("\t")
            if not section:
                raise ParseError(f"line {n}: a block before the first heading.")
            year = year.strip()
            if year != "~" and not (year.isdigit() and len(year) == 4):
                raise ParseError(f"line {n}: {year!r} is neither a year nor ~.")
            body = body.strip()
            items.append((section, Entry("text", body if year == "~" else f"{year}\t{body}")))
        else:
            raise ParseError(f"line {n}: not a heading, a block or a continuation.")
    known = {s for s, _ in items}
    for name, sections in referents.items():
        unknown = [s for s in sections if s not in known]
        if unknown:
            raise ParseError(f"referent {name!r} names unknown section {unknown[0]!r}.")
    return items, referents


def build(src: Path, out: Path) -> int:
    items, wanted = parse(src.read_text("utf-8"))
    existing: list[Block] = []
    if out.exists():
        existing = [Block(b["id"], b["kind"], b["text"]) for b in json.loads(out.read_text("utf-8"))["blocks"]]
    result = import_blocks([e for _, e in items], existing)

    referents = []
    for rid in ("short", "middle", "full"):
        if rid == "full":
            ids = [b.id for b in result.blocks]
        else:
            if rid not in wanted:
                continue
            ids = [b.id for (sec, _), b in zip(items, result.blocks) if sec in wanted[rid]]
        referents.append({"id": rid, "name": REFERENT_NAMES[rid], "blocks": ids})

    data = {"blocks": [b.to_api() for b in result.blocks], "referents": referents}
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", "utf-8")
    print(f"{len(result.blocks)} blocks: {len(result.kept)} kept, {len(result.added)} added, "
          f"{len(result.removed)} removed -> {out}")
    for b in result.removed:
        print(f"  removed (id kept in existing sets): {b.id} {b.text[:60]!r}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    try:
        sys.exit(build(Path(sys.argv[1]), Path(sys.argv[2])))
    except (ParseError, CvImportError) as exc:
        sys.exit(f"build_cv_master: {exc}")
