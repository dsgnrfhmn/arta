"""pipeline/cv_blocks: stable ids across re-import of an edited master."""

import uuid

import pytest

from pipeline.cv_blocks import CvImportError, Entry, import_blocks
from sources.cv_source import Block

MASTER = [
    Entry("heading", "Education"),
    Entry("text", "2021\tMA in Painting, Northbay | XX"),
    Entry("heading", "Residencies"),
    Entry("text", "2024\tresidency MaltAir, Ebeltoft | DK"),
]


def ids(blocks):
    return [b.id for b in blocks]


def test_first_import_gives_every_block_its_own_uuid():
    r = import_blocks(MASTER)
    assert [(b.kind, b.text) for b in r.blocks] == [(e.kind, e.text) for e in MASTER]
    assert all(str(uuid.UUID(i)) == i for i in ids(r.blocks))
    assert len(set(ids(r.blocks))) == 4
    assert r.added == r.blocks and r.kept == [] and r.removed == []


def test_same_text_keeps_id_new_text_gets_new_id_removed_is_reported():
    first = import_blocks(MASTER)
    edited = [MASTER[0], Entry("text", "2021\tMA in Painting, Northbay | XX"), MASTER[2],
              Entry("text", "2025\tresidency Somewhere, Berlin | DE")]
    second = import_blocks(edited, first.blocks)
    assert ids(second.blocks)[:3] == ids(first.blocks)[:3]
    assert second.blocks[3].id not in ids(first.blocks)
    assert [b.text for b in second.added] == ["2025\tresidency Somewhere, Berlin | DE"]
    assert [b.id for b in second.removed] == [first.blocks[3].id]
    assert len(second.kept) == 3


def test_a_reorder_does_not_change_ids():
    first = import_blocks(MASTER)
    second = import_blocks(list(reversed(MASTER)), first.blocks)
    assert ids(second.blocks) == list(reversed(ids(first.blocks)))
    assert second.added == [] and second.removed == []


def test_repeated_text_is_matched_in_order_and_keeps_distinct_ids():
    entries = [Entry("heading", "2024"), Entry("text", "a"), Entry("heading", "2024"), Entry("text", "b")]
    first = import_blocks(entries)
    assert first.blocks[0].id != first.blocks[2].id
    second = import_blocks(entries, first.blocks)
    assert ids(second.blocks) == ids(first.blocks)
    third = import_blocks(entries[:3], first.blocks)  # one "2024" and "b" lost
    assert ids(third.blocks) == ids(first.blocks)[:3]
    assert [b.text for b in third.removed] == ["b"]


def test_every_existing_set_still_resolves_after_an_edit():
    """A set holds block ids. After re-import each id is either still in the master
    or reported as removed, so nothing is silently orphaned."""
    first = import_blocks(MASTER)
    set_ids = ids(first.blocks)
    edited = [MASTER[0], Entry("text", "2021\tMA in Painting, Northbay | XX (edited)"), MASTER[2]]
    second = import_blocks(edited, first.blocks)
    known = set(ids(second.blocks)) | {b.id for b in second.removed}
    assert set(set_ids) <= known
    assert first.blocks[1].id in {b.id for b in second.removed}


def test_markup_is_stripped_and_the_year_tab_survives():
    r = import_blocks([Entry("text", "2024\t<b>exhibition</b> ‮ x")])
    assert r.blocks[0].text == "2024\texhibition  x"


@pytest.mark.parametrize("entry,message", [
    (Entry("banner", "x"), "kind"),
    (Entry("text", "   "), "empty"),
    (Entry("text", "x" * 20_001), "longer"),
])
def test_a_bad_entry_raises_and_names_it(entry, message):
    with pytest.raises(CvImportError, match=message):
        import_blocks([MASTER[0], entry])


def test_existing_blocks_are_not_mutated():
    existing = [Block(str(uuid.uuid4()), "text", "x")]
    import_blocks([Entry("heading", "x")], existing)
    assert existing[0].kind == "text"


# --- scripts/build_cv_master.py over the Figma transcription --------------------

import importlib.util
import json
from pathlib import Path

from sources.local_cv import LocalCv

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("build_cv_master", ROOT / "scripts" / "build_cv_master.py")
build_cv_master = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build_cv_master)
TXT = ROOT / "tests" / "fixtures" / "cv" / "figma_cv.txt"


def test_the_figma_master_builds_and_loads_through_the_fixture_source(tmp_path):
    out = tmp_path / "m.json"
    build_cv_master.build(TXT, out)
    src = LocalCv(out)
    assert [r.id for r in src.list_referents()] == ["short", "middle", "full"]
    full, short = src.get_blocks("full"), src.get_blocks("short")
    assert len(full) > 60 and {b.id for b in short} < {b.id for b in full}
    assert full[0].kind == "heading" and full[0].text == "JANE ROE"
    assert any(b.text.startswith("2024\t") and "\n" in b.text for b in full)


def test_rebuilding_the_unchanged_master_keeps_every_id_and_an_edit_keeps_the_rest(tmp_path, capsys):
    out = tmp_path / "m.json"
    build_cv_master.build(TXT, out)
    before = json.loads(out.read_text("utf-8"))["blocks"]
    build_cv_master.build(TXT, out)
    assert json.loads(out.read_text("utf-8"))["blocks"] == before

    edited = tmp_path / "edited.txt"
    edited.write_text(TXT.read_text("utf-8").replace("Example Quarterly # 12, Northbay", "Example Quarterly # 13, Northbay"), "utf-8")
    capsys.readouterr()
    build_cv_master.build(edited, out)
    after = json.loads(out.read_text("utf-8"))["blocks"]
    changed = [(a, b) for a, b in zip(before, after) if a != b]
    assert len(changed) == 1 and changed[0][0]["id"] != changed[0][1]["id"]
    assert "removed" in capsys.readouterr().out


@pytest.mark.parametrize("text,message", [
    ("2024\tx\n", "before the first heading"),
    ("# A\nxx\n", "not a heading"),
    ("# A\n24\tx\n", "neither a year"),
    ("# A\n+\tx\n", "continuation"),
    ("@short: B\n# A\n", "unknown section"),
])
def test_a_master_that_does_not_parse_raises_and_writes_nothing(tmp_path, text, message):
    src, out = tmp_path / "in.txt", tmp_path / "out.json"
    src.write_text(text, "utf-8")
    with pytest.raises(build_cv_master.ParseError, match=message):
        build_cv_master.build(src, out)
    assert not out.exists()
