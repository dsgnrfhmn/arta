"""pipeline/odt_cv and sources/drive_cv: the master CV (a Google Doc), cached, fail closed."""

import io
import json
import zipfile

import pytest

from pipeline.cv_blocks import CvImportError
from pipeline.odt_cv import parse
from sources.drive_cv import DriveCv
from sources.project_source import NotFound, SourceError

NS = ('xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
      'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"')


def odt(body: str, prefix: str = "") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("content.xml", f'{prefix}<office:document-content {NS}><office:body><office:text>{body}</office:text></office:body></office:document-content>')
    return buf.getvalue()


def p(text=""):
    return f"<text:p>{text}</text:p>"


def lst(*items):
    return "<text:list>" + "".join(f"<text:list-item>{p(i)}</text:list-item>" for i in items) + "</text:list>"


# The shape of the Google Doc export: no headings, no tables, capitals for sections,
# a paragraph per year, list items under it.
BODY = (
    p("Base CURRICULUM VITAE")
    + p("JANE DOE") + p("Born 1997; X.") + p("jane@example.org") + p()
    + p("BIO") + p() + p("An artist, who<text:line-break/>makes things.") + p()
    + p("EDUCATION")
    + p("2024") + lst("Course A; Place A", "Course B")
    + p() + p("2015-2018") + lst("BA, Kyiv")
    + p() + p("Selected talks, symposiums") + p("2025") + p("Talk T, Berlin | DE") + p() + p()
    + p("GRANTS")
    + p("2024, Grant G; Berlin | DE") + p() + p("2023,  Grant H")
    + p("COLLECTIONS") + lst("Museum M", "Club C")
)


def entries(parsed):
    return [(s, e.kind, e.text) for s, e in parsed.items]


def test_parse_reads_the_shape_from_the_text():
    assert entries(parse(odt(BODY))) == [
        ("JANE DOE", "heading", "JANE DOE"),
        ("JANE DOE", "text", "Born 1997; X."),
        ("JANE DOE", "text", "jane@example.org"),
        ("BIO", "heading", "BIO"),
        ("BIO", "text", "An artist, who makes things."),
        ("EDUCATION", "heading", "EDUCATION"),
        ("EDUCATION", "text", "2024\tCourse A; Place A"),
        ("EDUCATION", "text", "2024\tCourse B"),              # the year above carries down
        ("EDUCATION", "text", "2015-2018\tBA, Kyiv"),
        ("EDUCATION", "text", "Selected talks, symposiums"),   # a sub-heading ends the year, keeps no year
        ("EDUCATION", "text", "2025\tTalk T, Berlin | DE"),
        ("GRANTS", "heading", "GRANTS"),
        ("GRANTS", "text", "2024\tGrant G; Berlin | DE"),     # an entry's own year wins
        ("GRANTS", "text", "2023\tGrant H"),
        ("COLLECTIONS", "heading", "COLLECTIONS"),
        ("COLLECTIONS", "text", "Museum M"),                  # no year in force: text as it stands
        ("COLLECTIONS", "text", "Club C"),
    ]


def test_a_year_does_not_leak_into_the_next_section():
    got = entries(parse(odt(p("A") + p("2020") + lst("x") + p("B") + lst("y"))))
    assert ("B", "text", "y") in got


def test_zero_width_characters_and_line_separators_are_cleaned():
    got = entries(parse(odt(p("S") + p("2024") + p("Talk at X‍"))))
    assert got[-1] == ("S", "text", "2024\tTalk at X")


def test_title_before_the_first_section_is_skipped_and_reported():
    parsed = parse(odt(BODY))
    assert not any("Base" in e.text for _, e in parsed.items)
    assert any("Base CURRICULUM VITAE" in s and "before the first section" in s for s in parsed.skipped)


@pytest.mark.parametrize("data,message", [
    (b"not a zip", "not an OpenDocument"),
    (odt(BODY, prefix='<!DOCTYPE x [<!ENTITY a "b">]>'), "DOCTYPE"),
    (odt(p("only loose text")), "no content"),
    (odt(""), "no content"),
])
def test_bad_masters_fail_loud(data, message):
    with pytest.raises(CvImportError, match=message):
        parse(data)


# --- DriveCv ----------------------------------------------------------------

class FakeDrive:
    def __init__(self, data, version="1", mime="application/vnd.google-apps.document", fail=None):
        self.data, self.version, self.mime, self.fail = data, version, mime, fail
        self.meta_calls = self.export_calls = 0
        self.export_mime = None

    def files(self):
        return self

    def get(self, **kw):
        self.meta_calls += 1
        return self._Req(self, {"id": "f", "mimeType": self.mime, "version": self.version})

    def export(self, **kw):
        self.export_calls += 1
        self.export_mime = kw.get("mimeType")
        return self._Req(self, self.data)

    class _Req:
        def __init__(self, outer, value):
            self.o, self.v = outer, value

        def execute(self):
            if self.o.fail:
                raise self.o.fail
            return self.v


def make(tmp_path, drive, ttl=0.0):
    return DriveCv("file-id-0123456789", tmp_path / "cache" / "cv.json", service=lambda: drive, ttl=ttl)


def test_every_referent_is_the_whole_master_with_the_same_ids(tmp_path):
    d = FakeDrive(odt(BODY))
    cv = make(tmp_path, d)
    assert [r.id for r in cv.list_referents()] == ["short", "middle", "full"]
    full = cv.get_blocks("full")
    assert len(full) == 17
    assert cv.get_blocks("short") == full and cv.get_blocks("middle") == full
    assert d.export_mime == "application/vnd.oasis.opendocument.text"
    with pytest.raises(NotFound):
        cv.get_blocks("huge")


def test_second_read_is_served_from_cache_without_exporting(tmp_path):
    d = FakeDrive(odt(BODY))
    cv = make(tmp_path, d)
    cv.get_blocks("full")
    cv.get_blocks("full")
    assert d.export_calls == 1
    fresh = make(tmp_path, d)                  # a restarted server: the cache file is enough
    fresh.get_blocks("full")
    assert d.export_calls == 1


def test_ttl_skips_even_the_metadata_call(tmp_path):
    d = FakeDrive(odt(BODY))
    cv = make(tmp_path, d, ttl=3600)
    cv.get_blocks("full")
    cv.get_blocks("full")
    assert d.meta_calls == 1


def test_changed_version_reimports_and_keeps_ids_of_unchanged_text(tmp_path):
    d = FakeDrive(odt(BODY))
    cv = make(tmp_path, d)
    before = {b.text: b.id for b in cv.get_blocks("full")}
    d.data, d.version = odt(BODY.replace("BA, Kyiv", "BA, Lviv")), "2"
    after = {b.text: b.id for b in cv.get_blocks("full")}
    assert d.export_calls == 2
    assert "2015-2018\tBA, Kyiv" not in after and "2015-2018\tBA, Lviv" in after
    assert all(after[t] == i for t, i in before.items() if t in after)     # unchanged text keeps its id
    assert after["2015-2018\tBA, Lviv"] not in before.values()


def test_drive_failure_fails_closed_and_does_not_serve_the_cache(tmp_path):
    d = FakeDrive(odt(BODY))
    make(tmp_path, d).get_blocks("full")
    d.fail = RuntimeError("invalid_grant: token abc.secret")
    with pytest.raises(SourceError) as exc:
        make(tmp_path, d).get_blocks("full")
    assert "secret" not in str(exc.value) and "Nothing stale" in str(exc.value)


def test_wrong_type_and_bad_content_are_source_errors(tmp_path):
    with pytest.raises(SourceError, match="Google Doc"):
        make(tmp_path, FakeDrive(odt(BODY), mime="application/pdf")).get_blocks("full")
    with pytest.raises(SourceError, match="cannot be imported"):
        make(tmp_path / "b", FakeDrive(b"junk")).get_blocks("full")


def test_cache_is_private_and_for_this_file_only(tmp_path):
    cv = make(tmp_path, FakeDrive(odt(BODY)))
    cv.get_blocks("full")
    assert (cv.cache_path.stat().st_mode & 0o777) == 0o600
    cached_ids = {b["id"] for b in json.loads(cv.cache_path.read_text())["blocks"]}
    other = DriveCv("another-file-0123456789", tmp_path / "cache" / "cv.json",
                    service=lambda: FakeDrive(odt(BODY)), ttl=0)
    assert cached_ids.isdisjoint({b.id for b in other.get_blocks("full")})
