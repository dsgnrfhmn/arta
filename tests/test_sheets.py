"""layout/sheets.py: which sheet layout each picture and text gets."""

from layout import sheets
from models.project import ImageRef, Project


LANDSCAPE, PORTRAIT, SQUARE = (1600, 1000), (1000, 1600), (1200, 1200)


def image(n: int, role: str = "work", name: str | None = None, author: str = "Jane Roe", place: str = "", year: str = "",
          size: tuple[int, int] = LANDSCAPE) -> ImageRef:
    return ImageRef(id=f"i{n}", name=name or f"Piece-{n}__x.jpg", group=None, kind="jpeg", size=1,
                    role=role, author=author, place=place, year=year, width=size[0], height=size[1])


def entry(works=(), exhibitions=()):
    return {"id": "e1", "project_id": "p", "works": [i.id for i in works],
            "exhibitions": [i.id for i in exhibitions], "text_amount": "short"}


def layouts(works=(), exhibitions=(), text=""):
    project = Project("p", "Harbour", images=tuple(works) + tuple(exhibitions),
                      descriptions={"en": text} if text else {})
    return [s["layout"] for s in sheets.entry_sheets(entry(works, exhibitions), project)]


def test_no_text_and_a_wide_first_work_open_with_band():
    assert layouts() == ["band"]
    assert layouts([image(0)]) == ["band"]
    assert layouts([image(0), image(1)]) == ["band", "full"]


def test_the_opening_follows_the_first_work_and_the_count():
    assert layouts([image(0, size=PORTRAIT)], text="Some text.") == ["side"]
    assert layouts([image(0, size=SQUARE)], text="Some text.") == ["side"]
    assert layouts([image(0)], text="Some text.") == ["figure-side"]
    assert layouts([image(0, size=(0, 0))], text="Some text.") == ["figure-side"]       # unknown counts as wide
    assert layouts([image(0, size=PORTRAIT)]) == ["side"]                                # tall without text
    assert layouts([image(n) for n in range(4)]) == ["grid-side"]


def test_the_opening_carries_title_year_and_the_text_that_fits():
    works = [image(0, size=PORTRAIT, year="2024")]
    project = Project("p", "Harbour", images=tuple(works), descriptions={"en": "First.\n\nSecond."})
    (opening,) = sheets.entry_sheets(entry(works), project)
    assert (opening["title"], opening["subtitle"], opening["paragraphs"]) == ("Harbour", "2024", ["First.", "Second."])
    assert [i["id"] for i in opening["images"]] == ["i0"] and opening["slots"] is None


def test_works_after_the_opening_go_four_to_a_grid_then_one_per_sheet_by_shape():
    nine = [image(n) for n in range(8)] + [image(8, size=PORTRAIT)]
    assert layouts(nine, text="t") == ["grid-side", "grid", "figure"]
    assert layouts([image(0, size=PORTRAIT), image(1), image(2, size=PORTRAIT)], text="t") == ["side", "full", "figure"]
    assert layouts([image(n) for n in range(10)], text="t") == ["grid-side", "grid", "full", "full"]


def test_grid_cells_fill_row_by_row_and_carry_their_address():
    works = [image(n) for n in range(9)]
    project = Project("p", "Series", images=tuple(works), descriptions={"en": "t"})
    got = sheets.entry_sheets(entry(works), project)
    assert [s["layout"] for s in got] == ["grid-side", "grid", "full"]
    assert [[c["image"]["id"] for c in sh["slots"]] for sh in got[:2]] == [["i0", "i1", "i2", "i3"], ["i4", "i5", "i6", "i7"]]
    assert [(sh["grid_page"], sh["grid"]) for sh in got[:2]] == [(0, {"cols": 2, "rows": 2}), (1, {"cols": 2, "rows": 2})]
    assert got[0]["title"] == "Series" and got[1]["title"] == "" and got[2]["grid_page"] is None


def test_no_picked_picture_is_dropped_or_repeated():
    works = [image(n, size=PORTRAIT if n % 3 else LANDSCAPE) for n in range(11)]
    shows = [image(20 + n, "exhibition", size=SQUARE if n % 2 else LANDSCAPE) for n in range(3)]
    project = Project("p", "Series", images=tuple(works + shows), descriptions={"en": "t"})
    got = [i["id"] for s in sheets.entry_sheets(entry(works, shows), project) for i in s["images"]]
    assert got == [i.id for i in works + shows]


def test_exhibition_photos_are_one_per_sheet_after_works_and_text():
    shows = [image(5, "exhibition"), image(6, "exhibition", size=PORTRAIT)]
    text = "\n\n".join(["word " * 100] * 3)
    assert layouts([image(0)], shows, text) == ["figure-side", "text", "full", "figure"]
    project = Project("p", "Series", images=tuple(shows))
    got = sheets.entry_sheets(entry(exhibitions=shows), project)
    assert got[1]["images"][0]["caption"]["prefix"] == "Exhibition view, "


def test_every_picture_says_whether_it_is_a_work_or_an_exhibition_view():
    works = [image(n) for n in range(6)]
    shows = [image(10, "exhibition"), image(11, "exhibition", size=PORTRAIT)]
    project = Project("p", "Series", images=tuple(works + shows))
    got = sheets.entry_sheets(entry(works, shows), project)
    roles = {i["id"]: i["role"] for s in got for i in s["images"]}
    roles.update({sl["image"]["id"]: sl["image"]["role"] for s in got for sl in (s.get("slots") or []) if sl["image"]})
    assert roles == {**{w.id: "work" for w in works}, **{e.id: "exhibition" for e in shows}}


def test_text_that_does_not_fit_the_opening_continues_on_text_sheets():
    text = "\n\n".join(["word " * 100] * 8)          # 4000 characters
    got = layouts([image(0)], text=text)
    assert got[0] == "figure-side" and got[1:] == ["text", "text"]
    assert all(sum(map(len, g)) <= sheets.TEXT_PER_SHEET for g in sheets.paragraphs(text))


def test_arrange_pads_the_last_page_and_keeps_pick_order():
    pics = [image(n) for n in range(5)]
    pages = sheets.arrange(pics, 2, 2)
    assert [[p.id if p else None for p in page] for page in pages] == [["i0", "i1", "i2", "i3"], ["i4", None, None, None]]
    assert sheets.arrange([], 2, 2) == []


def test_a_single_paragraph_longer_than_a_sheet_is_split_on_spaces():
    groups = sheets.paragraphs("lorem " * 600)
    assert len(groups) > 1
    assert all(len(para) <= sheets.TEXT_PER_SHEET for g in groups for para in g)
    assert " ".join(p for g in groups for p in g).split() == ("lorem " * 600).split()


def test_captions_use_file_name_title_year_place_author():
    works = [image(0, name="Harbour-1__Jane-Roe__Annex__2024.jpg", place="Annex", year="2024")]
    project = Project("p", "Series", images=tuple(works))
    cap = sheets.entry_sheets(entry(works), project)[0]["images"][0]["caption"]
    assert cap == {"prefix": "", "label": "Harbour", "suffix": ", 2024", "lines": ["Annex © Jane Roe"]}


def test_exhibition_caption_says_exhibition_view_and_hides_not_specified_author():
    shows = [image(0, "exhibition", name="IW-Harbour-3__not-specified.jpg", author="not-specified")]
    project = Project("p", "Series", images=tuple(shows))
    sheet = sheets.entry_sheets(entry(exhibitions=shows), project)[1]
    assert sheet["layout"] == "full"
    assert sheet["images"][0]["caption"] == {"prefix": "Exhibition view, ", "label": "Harbour", "suffix": "", "lines": []}


def test_untitled_picture_is_captioned_with_the_project_name():
    works = [image(0, name="IMG_5297__not-specified.heic", author="not-specified")]
    project = Project("p", "Series", images=tuple(works))
    assert sheets.entry_sheets(entry(works), project)[0]["images"][0]["caption"]["label"] == "Series"


def test_every_sheet_names_its_entry_and_project():
    works = [image(0), image(1)]
    project = Project("p", "Series", images=tuple(works))
    got = sheets.entry_sheets(entry(works), project)
    assert {(s["entry_id"], s["project_id"]) for s in got} == {("e1", "p")}


def test_a_missing_project_is_one_missing_sheet():
    assert [s["layout"] for s in sheets.entry_sheets(entry(), None)] == ["missing"]


def test_ids_that_are_not_in_the_project_are_skipped():
    project = Project("p", "Series", images=(image(0),))
    e = {**entry(), "works": ["i0", "gone"]}
    assert [s["layout"] for s in sheets.entry_sheets(e, project)] == ["band"]


# --- the set's own layout plan (dsgnrfhmn, 2026-10-24) ------------------------------

def planned(plan, works, text="Some text."):
    project = Project("p", "Harbour", images=tuple(works), descriptions={"en": text} if text else {})
    return sheets.entry_sheets({**entry(works), "layout_plan": plan}, project)


def test_a_plan_sets_the_opening_and_then_one_layout_per_works_sheet():
    works = [image(n) for n in range(9)]
    out = planned(["side", "pair", "triple", "full"], works)   # 1 + 2 + 3 + 1, then 2 left: one per sheet
    works_sheets = [s for s in out if s.get("plan_index") is not None]
    assert [s["layout"] for s in works_sheets] == ["side", "pair", "triple", "full", "full", "full"]
    assert [s["plan_index"] for s in works_sheets] == list(range(len(works_sheets)))
    used = [i["id"] for s in works_sheets for i in s["images"]]
    assert used == [w.id for w in works]                 # every work once, in pick order


def test_after_the_plan_ends_the_automatic_choice_takes_over():
    works = [image(n) for n in range(7)]
    out = planned(["figure-side", "pair"], works)        # 1 + 2, then 4 left: one grid
    assert [s["layout"] for s in out if s.get("plan_index") is not None] == ["figure-side", "pair", "grid"]


def test_pair_and_triple_are_rows_with_addressed_cells():
    out = planned(["side", "triple"], [image(n) for n in range(3)])  # 1 + only 2 for the triple
    triple = out[1]
    assert triple["layout"] == "triple" and triple["grid"] == {"cols": 3, "rows": 1}
    assert [s["cell"] for s in triple["slots"]] == [0, 1, 2] and triple["slots"][2]["image"] is None


def test_band_chosen_with_text_sends_the_text_to_text_sheets():
    out = planned(["band"], [image(0)], text="A paragraph that does not sit on a band.")
    assert [s["layout"] for s in out] == ["band", "text"] and out[0]["paragraphs"] == []


def test_unknown_names_are_ignored_and_a_short_set_stops_the_plan():
    out = planned(["nonsense", "pair", "bogus", "grid", "grid"], [image(n) for n in range(3)])
    assert [s["layout"] for s in out if s.get("plan_index") is not None][:2] == ["figure-side", "pair"]
    assert len([s for s in out if s.get("plan_index") is not None]) == 2


def test_without_a_plan_nothing_changes_but_the_index():
    works = [image(n) for n in range(6)]
    out = planned([], works)
    assert [s["layout"] for s in out] == ["grid-side", "full", "full"]
    assert [s["plan_index"] for s in out if "plan_index" in s] == list(range(3))


def test_the_sets_own_texts_win_on_the_sheets():
    works = [image(0, year="2024"), image(1)]
    project = Project("p", "Harbour", images=tuple(works), descriptions={"en": "Original text."})
    e = {**entry(works), "title_override": "New title", "subtitle_override": "2020-2024",
         "text_override": "Edited text.", "caption_overrides": {"i1": "Edited caption\nsecond line"}}
    out = sheets.entry_sheets(e, project)
    assert (out[0]["title"], out[0]["subtitle"], out[0]["paragraphs"]) == ("New title", "2020-2024", ["Edited text."])
    assert out[0]["entry_text"] == "Edited text."
    caption = next(i["caption"] for s in out for i in s["images"] if i["id"] == "i1")
    assert caption == {"prefix": "", "label": "Edited caption", "suffix": "", "lines": ["second line"], "edited": True}
    untouched = next(i["caption"] for s in out for i in s["images"] if i["id"] == "i0")
    assert untouched == sheets._caption(works[0], project) and "edited" not in untouched


def test_exhibition_views_follow_their_own_plan_then_one_per_sheet():
    works = [image(0)]
    ex = [image(n, role="exhibition") for n in range(1, 7)]
    project = Project("p", "Harbour", images=tuple(works) + tuple(ex), descriptions={"en": "Text."})
    e = {**entry(works, ex), "exhibition_plan": ["pair", "triple"]}
    out = [s for s in sheets.entry_sheets(e, project) if "exhibition_index" in s]
    assert [s["layout"] for s in out] == ["pair", "triple", "full"]
    assert [s["exhibition_index"] for s in out] == [0, 1, 2]
    assert [i["id"] for s in out for i in s["images"]] == [i.id for i in ex]          # each view once, in order
    assert all(i["caption"]["prefix"] == "Exhibition view, " for s in out for i in s["images"])
    assert all("plan_index" not in s for s in out)                                       # the works menu does not address them
