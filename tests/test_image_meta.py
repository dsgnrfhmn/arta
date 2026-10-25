"""sources/image_meta.py: the one reader of the file-name convention."""

import pytest

from sources.image_meta import ImageMeta, Registry, caption, load_registry, parse_image_meta, title_of


@pytest.mark.parametrize("name,folders,expected", [
    ("Lifes-1.7__John-Doe__Annex__2024.jpg", (), ("work", "John Doe", "Annex", "2024")),
    ("IW-1__Jane-Roe__Gallery-One__2023.jpg", (), ("exhibition", "Jane Roe", "Gallery One", "2023")),
    ("Detail-2__Jane-Roe.jpg", (), ("exhibition", "Jane Roe", "", "")),
    ("IMG_5297__not-specified.heic", (), ("work", "not-specified", "", "")),
    ("IMG_0419__not-specified__Castle-Hall__2023.jpg", (), ("work", "not-specified", "Castle Hall", "2023")),
    ("Harbour-1__Jane-Roe__2024.jpg", ("2024, Exposition in Northbay",), ("exhibition", "Jane Roe", "Northbay", "2024")),
    ("IMG_1.jpg", ("Photo of Exposition", "gallery One, Berlin (2022)"), ("exhibition", "not-specified", "gallery One, Berlin", "2022")),
    ("IMG_2.jpg", ("Views__Annex (2021)",), ("work", "not-specified", "Annex", "2021")),
    ("IMG_3.jpg", ("Field notes (Digital painting)",), ("work", "not-specified", "", "")),
    ("1.jpg", ("Paintings",), ("work", "not-specified", "", "")),
    ("IWork.jpg", (), ("work", "not-specified", "", "")),   # not the IW- prefix
    ("3 SOLD__not-specified.jpg", (), ("work", "not-specified", "", "")),
])
def test_parse(name, folders, expected):
    m = parse_image_meta(name, folders)
    assert (m.role, m.author, m.place, m.year) == expected


REGISTRY = Registry.from_dict({
    "authors": {"Jane-Roe": ["jane-rooe"], "John-Doe": []},
    "places": {"Gallery-One": ["galleryone"], "Castle-Hall": []},
})


# Names as they are on Drive, never renamed: any field order, typos, glued years.
@pytest.mark.parametrize("name,folders,expected", [
    ("IW-1__Connection__Gallery-One__Jane-Roe.jpg", (), ("exhibition", "Jane Roe", "Gallery One", "")),
    ("Diary-1.1__Gallery-One__Jane-Rooe-2026.jpg", (), ("work", "Jane Roe", "Gallery One", "2026")),
    ("Shelter-1.1__John-Doe-2021.jpg", (), ("work", "John Doe", "", "2021")),
    ("IW-1-Cloud__GalleryOne__Jane-Roe.jpg", (), ("exhibition", "Jane Roe", "Gallery One", "")),
    ("Cloud-1.1__Jane-Roejpg.heif", (), ("work", "Jane Roe", "", "")),
    ("IW__Tennis-1_Jane-Roe.heif", (), ("exhibition", "Jane Roe", "", "")),
    ("Detail__Presence-1__John-Doe_Castle-Hall-2024.jpg", (), ("exhibition", "John Doe", "Castle Hall", "2024")),
    ("Detail-1-Jane-Roe__GalleryOne.jpg", (), ("exhibition", "Jane Roe", "Gallery One", "")),
    ("(Run)__View-of-Show__group-show__Gallery-One__2023__John-Doe.2.jpg", (), ("work", "John Doe", "Gallery One", "2023")),
    ("Run-3__2022__Oil-on-canvas__200x200-cm_John-Doe.jpg", (), ("work", "John Doe", "", "2022")),
    ("IMG_7.jpg", ("Views__GalleryOne (2023)",), ("work", "not-specified", "Gallery One", "2023")),
    # nothing in the registry: read by position, as without one
    ("Lifes-1.7__Mary-Major__Annex__2024.jpg", (), ("work", "Mary Major", "Annex", "2024")),
    ("IMG_5297__not-specified.heic", (), ("work", "not-specified", "", "")),
])
def test_parse_with_registry(name, folders, expected):
    m = parse_image_meta(name, folders, REGISTRY)
    assert (m.role, m.author, m.place, m.year) == expected


def test_load_registry_refuses_another_shape(tmp_path):
    good = tmp_path / "good.json"
    good.write_text('{"authors": {"Jane-Roe": ["jane-rooe"]}, "places": {}}')
    assert load_registry(good).find("Jane-Rooe") == ("author", "Jane-Roe")
    bad = tmp_path / "bad.json"
    bad.write_text('{"authors": ["Jane-Roe"]}')
    with pytest.raises(ValueError):
        load_registry(bad)


def test_caption_leaves_unknown_parts_out_but_keeps_not_specified():
    assert caption(ImageMeta("work", "Jane Roe", "Annex", "2024")) == "Jane Roe, Annex, 2024"
    assert caption(ImageMeta("work", "not-specified", "", "")) == "not-specified"
    assert caption(ImageMeta("work", "Jane Roe", "", "2023")) == "Jane Roe, 2023"


@pytest.mark.parametrize("name,expected", [
    ("Harbour-1__Jane-Roe__Gallery-One__2024.jpg", "Harbour"),
    ("IW-Field-Notes-3__Jane-Roe.jpg", "Field Notes"),
    ("Detail-2__Jane-Roe.jpg", ""),
    ("Lifes-1.7__John-Doe__Annex__2024.jpg", "Lifes"),
    ("IMG_5297__not-specified.heic", ""),
    ("1.jpg", ""),
    ("20230725_IMG_4441__not-specified.jpg", ""),
    ("2A0E4167-A7C8-4CBF-AF58-212AB834A264__Jane-Roe.jpg", ""),
    ("Image-Study-2__Jane-Roe.jpg", "Image Study"),
])
def test_title_of(name, expected):
    assert title_of(name) == expected
