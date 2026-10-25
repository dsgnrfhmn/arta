"""The portfolio cover: page 1 of the portfolio PDF, opening every set (sources/portfolio_cover.py)."""

import hashlib
import io
import json
import shutil
import stat
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from PIL import Image

from core.config import Config, ConfigError, example_data, parse
from core.db import Database
from core.seed import seed_opportunities, seed_portfolio_sets
from sources.local_projects import LocalProjects
from sources.portfolio_cover import DriveCover, FileCover, render_first_page
from sources.project_source import SourceError
from web import server as s

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"
needs_pdftoppm = pytest.mark.skipif(shutil.which("pdftoppm") is None, reason="poppler-utils not installed")


def pdf(color="red", pages=2):
    """A synthetic PDF of 842x599 pages; page 1 is `color`, the rest white."""
    frames = [Image.new("RGB", (842, 599), color)] + [Image.new("RGB", (842, 599), "white")] * (pages - 1)
    buf = io.BytesIO()
    frames[0].save(buf, "PDF", save_all=True, append_images=frames[1:], resolution=72)
    return buf.getvalue()


@needs_pdftoppm
def test_page_one_is_rendered_wide_and_private(tmp_path):
    out = tmp_path / "cache" / "c.jpg"
    render_first_page(pdf("red"), out)
    img = Image.open(out)
    assert img.size[0] == 1684 and abs(img.size[0] / img.size[1] - 842 / 599) < 0.01
    r, g, b = img.convert("RGB").getpixel((800, 600))
    assert r > 200 and g < 60 and b < 60                     # page 1 (red), not page 2 (white)
    assert stat.S_IMODE(out.stat().st_mode) == 0o600


def test_a_broken_pdf_fails_loudly(tmp_path):
    with pytest.raises(SourceError):
        render_first_page(b"not a pdf", tmp_path / "c.jpg")


@needs_pdftoppm
def test_file_cover_caches_by_content_and_drops_the_old_page(tmp_path):
    src, cache = tmp_path / "portfolio.pdf", tmp_path / "cover"
    src.write_bytes(pdf("red"))
    cover = FileCover(src, cache)
    first = cover.current()
    assert first.path.exists() and cover.current() == first
    src.write_bytes(pdf("blue"))
    second = cover.current()
    assert second.fingerprint != first.fingerprint and not first.path.exists()
    assert {p.name for p in cache.iterdir()} == {second.path.name, f"{second.fingerprint}-photo.jpg"}


class FakeFiles:
    def __init__(self, meta):
        self.meta, self.gets = meta, 0

    def get(self, **kw):
        self.gets += 1
        meta = self.meta
        class Req:
            def execute(_):
                if isinstance(meta, Exception):
                    raise meta
                return meta
        return Req()


class FakeDrive:
    def __init__(self, meta):
        self._files = FakeFiles(meta)

    def files(self):
        return self._files


def drive_cover(tmp_path, meta, body=None, clock=lambda: 0.0):
    svc = FakeDrive(meta)
    cover = DriveCover("FILEID1234567", tmp_path / "cover", service=lambda: svc, ttl=30, clock=clock)
    downloads = []
    cover._download = lambda _svc: downloads.append(1) or (body if body is not None else pdf())
    return cover, svc, downloads


@needs_pdftoppm
def test_drive_cover_downloads_once_per_md5(tmp_path):
    now = [0.0]
    meta = {"id": "FILEID1234567", "mimeType": "application/pdf", "md5Checksum": "aaa", "size": "1000"}
    cover, svc, downloads = drive_cover(tmp_path, meta, clock=lambda: now[0])
    first = cover.current()
    assert first.fingerprint == "aaa" and first.path.exists() and downloads == [1]
    now[0] = 100.0
    assert cover.current() == first and downloads == [1]            # same md5: asked, not downloaded
    meta["md5Checksum"] = "bbb"
    now[0] = 200.0
    assert cover.current().fingerprint == "bbb" and downloads == [1, 1] and not first.path.exists()


def test_drive_cover_refuses_what_is_not_a_pdf(tmp_path):
    cover, _, downloads = drive_cover(tmp_path, {"mimeType": "image/jpeg", "md5Checksum": "a", "size": "1"})
    with pytest.raises(SourceError, match="not a PDF"):
        cover.current()
    assert downloads == []


def test_drive_cover_fails_closed_without_detail(tmp_path):
    cover, _, _ = drive_cover(tmp_path, RuntimeError("token ya29.secret leaked"))
    with pytest.raises(SourceError) as info:
        cover.current()
    assert "Nothing stale is served" in str(info.value) and "ya29" not in str(info.value)


def conf(**sections):
    """The example configuration with some keys replaced (tests/test_config.py)."""
    data = example_data()
    data["server"]["data_dir"] = "/var/lib/arta-test"
    for section, values in sections.items():
        data[section].update(values)
    return data


def test_config_cover_settings():
    google = {"token_file": "/srv/token.json"}
    assert parse(conf()).cover_drive_file is None and parse(conf()).cover_file is None
    assert parse(conf(portfolio_cover={"drive_file": "0000EXAMPLE0000"}, google=google)).cover_drive_file == "0000EXAMPLE0000"
    assert parse(conf(portfolio_cover={"file": "/srv/p.pdf"})).cover_file == Path("/srv/p.pdf")
    for bad in ({"drive_file": "https://drive.google.com/x"},
                {"file": "p.pdf"},
                {"drive_file": "0000EXAMPLE0000", "file": "/srv/p.pdf"}):
        with pytest.raises(ConfigError):
            parse(conf(portfolio_cover=bad, google=google))


# --- HTTP -------------------------------------------------------------------

@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "arta.sqlite3")
    d.migrate()
    seed_opportunities(d, FIXTURES)
    seed_portfolio_sets(d, FIXTURES, LocalProjects(FIXTURES / "projects"))
    return d


def serve(db, tmp_path, cover):
    cfg = Config(updates="fixtures", projects="fixtures", cv="fixtures", llm="mock", port=0, data_dir=tmp_path / "data")
    server = s.make_server(cfg, db, port=0, projects=LocalProjects(FIXTURES / "projects"), cover=cover)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def call(server, path, headers=None):
    req = urllib.request.Request(f"http://127.0.0.1:{server.server_address[1]}{path}", headers=headers or {})
    if not req.has_header("Origin"):   # a browser sends it on every write
        req.add_header("Origin", "http://" + req.host)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


@needs_pdftoppm
def test_every_set_opens_with_the_cover(db, tmp_path):
    src = tmp_path / "portfolio.pdf"
    src.write_bytes(pdf("red"))
    server = serve(db, tmp_path, FileCover(src, tmp_path / "cover"))
    try:
        items = json.loads(call(server, "/api/sets/portfolio")[2])["items"]
        sid = next(i["id"] for i in items if i["entry_count"])
        empty = next(i["id"] for i in items if not i["entry_count"])
        set_out = json.loads(call(server, f"/api/sets/portfolio/{sid}")[2])
        preview = json.loads(call(server, f"/api/sets/portfolio/{sid}/preview")[2])
        assert preview["pages"][0]["layout"] == "cover" and preview["pages"][0]["cover"]
        assert preview["page_count"] == set_out["pages"] == len(preview["pages"])
        assert json.loads(call(server, f"/api/sets/portfolio/{empty}/preview")[2])["pages"] == []
        status, headers, body = call(server, "/api/portfolio/cover")
        assert status == 200 and headers["Content-Type"] == "image/jpeg" and body[:2] == b"\xff\xd8"
        assert call(server, "/api/portfolio/cover", {"If-None-Match": headers["ETag"]})[0] == 304
    finally:
        server.shutdown()
        server.server_close()


def test_a_cover_that_cannot_be_read_is_a_note_not_a_broken_preview(db, tmp_path):
    class Broken:
        def current(self):
            raise SourceError("Drive could not be read (RefreshError). Nothing stale is served.")
    server = serve(db, tmp_path, Broken())
    try:
        items = json.loads(call(server, "/api/sets/portfolio")[2])["items"]
        sid = next(i["id"] for i in items if i["entry_count"])
        status, _, body = call(server, f"/api/sets/portfolio/{sid}/preview")
        first = json.loads(body)["pages"][0]
        assert status == 200 and first["layout"] == "cover" and first["cover"] is None and "Nothing stale" in first["note"]
        assert call(server, "/api/portfolio/cover")[0] == 502
    finally:
        server.shutdown()
        server.server_close()


def test_without_a_cover_nothing_changes(db, tmp_path):
    server = serve(db, tmp_path, None)
    try:
        items = json.loads(call(server, "/api/sets/portfolio")[2])["items"]
        sid = next(i["id"] for i in items if i["entry_count"])
        assert json.loads(call(server, f"/api/sets/portfolio/{sid}/preview")[2])["pages"][0]["layout"] != "cover"
        assert call(server, "/api/portfolio/cover")[0] == 404
    finally:
        server.shutdown()
        server.server_close()


@needs_pdftoppm
def test_the_photo_of_page_one_is_extracted_as_stored(tmp_path):
    src = tmp_path / "portfolio.pdf"
    src.write_bytes(pdf("red"))
    cover = FileCover(src, tmp_path / "cover").current()
    assert cover.photo is not None and cover.photo.read_bytes()[:2] == b"\xff\xd8"
    assert Image.open(cover.photo).size == (842, 599)                 # the page image of the synthetic PDF


def test_a_pdf_without_an_image_has_no_photo_and_is_not_read_again(tmp_path, monkeypatch):
    import sources.portfolio_cover as pc
    calls = []
    monkeypatch.setattr(pc, "render_first_page", lambda pdf, out: pc._write_private(out, b"\xff\xd8page"))
    monkeypatch.setattr(pc, "extract_photo", lambda pdf, out: calls.append(1) and False)
    first = pc._ensure(lambda: b"%PDF", tmp_path, "fp")
    second = pc._ensure(lambda: (_ for _ in ()).throw(AssertionError("read again")), tmp_path, "fp")
    assert first.photo is None and second.photo is None and calls == [1]


def test_cover_text_is_checked_never_guessed(tmp_path):
    import json as _json
    from sources.portfolio_cover import load_cover_text
    good = {"title": "PORTFOLIO", "name": "Jane Roe", "details": ["1990 b. X"], "heading": "BIO",
            "paragraphs": ["One.", "Two."], "contacts": ["jane@example.org"]}
    f = tmp_path / "cover.json"
    f.write_text(_json.dumps(good))
    assert load_cover_text(f) == good
    for bad in ({**good, "extra": "x"}, {k: v for k, v in good.items() if k != "name"}, {**good, "details": "x"},
                {**good, "contacts": [1]}):
        f.write_text(_json.dumps(bad))
        with pytest.raises(SourceError):
            load_cover_text(f)
    f.write_text("{not json")
    with pytest.raises(SourceError):
        load_cover_text(f)


@needs_pdftoppm
def test_a_text_cover_carries_its_words_and_the_photo(db, tmp_path):
    import json as _json
    src = tmp_path / "portfolio.pdf"
    src.write_bytes(pdf("red"))
    words = {"title": "PORTFOLIO", "name": "Jane Roe", "details": ["1990 b. X"], "heading": "BIO",
             "paragraphs": ["One."], "contacts": ["jane@example.org"]}
    (tmp_path / "cover.json").write_text(_json.dumps(words))
    cfg = Config(updates="fixtures", projects="fixtures", cv="fixtures", llm="mock", port=0, data_dir=tmp_path / "data", cover_text=tmp_path / "cover.json")
    server = s.make_server(cfg, db, port=0, projects=LocalProjects(FIXTURES / "projects"),
                           cover=FileCover(src, tmp_path / "cover"))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        items = json.loads(call(server, "/api/sets/portfolio")[2])["items"]
        sid = next(i["id"] for i in items if i["entry_count"])
        first = json.loads(call(server, f"/api/sets/portfolio/{sid}/preview")[2])["pages"][0]
        assert first["layout"] == "cover" and first["texts"] == words and first["photo"] is True
        status, headers, body = call(server, "/api/portfolio/cover/photo")
        assert status == 200 and body[:2] == b"\xff\xd8" and headers["ETag"].endswith('-photo"')
    finally:
        server.shutdown()
        server.server_close()
