"""sources/drive_projects: the Drive project source, cached, refreshed only on request."""

import io
import json
import re
import zipfile
from pathlib import Path

import pytest

from sources import drive_projects as dp
from sources.drive_projects import DriveProjects
from sources.image_meta import Registry
from sources.project_source import NotFound, SourceError

JPEG = b"\xff\xd8\xff\xe0" + b"\0" * 32
PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 32
W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


# Cyrillic letters only, no words: input for the code paths that handle a second script.
CYRILLIC = " ".join("".join(chr(0x0430 + (7 * i + j) % 32) for j in range(6)) for i in range(4))


def docx(text: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", f"<w:document {W}><w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>")
    return buf.getvalue()


def folder(fid, name, children):
    return {"id": fid, "name": name, "mimeType": dp.FOLDER, "children": children}


def file(fid, name, mime, data=b"", version="v1", **extra):
    return {"id": fid, "name": name, "mimeType": mime, "data": data, "md5Checksum": version, "size": str(len(data)), **extra}


def tree():
    return folder("root", "Projects", [
        folder("cat1", "Main", [
            folder("p1", "Sea  ", [
                file("i2", "Sea-2__Jane-Roe__2023.jpg", "image/jpeg", JPEG,
                     imageMediaMetadata={"width": 400, "height": 300, "rotation": 1}),
                file("i1", "IW-1__Gallery-One__Jane-Rooe-2024.jpg", "image/jpeg", JPEG),
                file("t1", "Sea.docx", dp.DOCX, docx("The sea project.")),
                file("g1", "Sea (second language)", dp.GOOGLE_DOC, docx(CYRILLIC)),
                file("v1", "clip.mov", "video/quicktime", b"x"),
                folder("s1", "2024, Exposition in Northbay", [
                    file("i3", "IMG_1", "application/octet-stream", PNG),
                ]),
            ]),
        ]),
        folder("cat2", "Performances", [
            folder("p2", "Embroidery", [file("i4", "a.jpg", "image/jpeg", JPEG)]),
        ]),
        folder("cat3", "Archive", [folder("p3", "Old", [])]),
    ])


class FakeRequest:
    def __init__(self, drive, fid, export=False):
        self.drive, self.fid, self.export, self.headers = drive, fid, export, {}

    def execute(self):
        self.drive.calls.append(("media", self.fid))
        data = self.drive.by_id[self.fid]["data"]
        m = re.match(r"bytes=0-(\d+)", self.headers.get("Range", ""))
        return data[: int(m.group(1)) + 1] if m else data


class FakeList:
    def __init__(self, drive, q):
        self.drive, self.q = drive, q

    def execute(self):
        self.drive.calls.append(("list", self.q))
        if self.drive.fail:
            raise ConnectionError("network down")
        if self.q == "trashed=false":
            kids = [n for n in self.drive.by_id.values() if n is not self.drive.root]
        else:
            kids = [self.drive.root]
        return {"files": [{k: v for k, v in c.items() if k not in ("children", "data")} for c in kids]}


class FakeDrive:
    def __init__(self, root):
        self.root, self.fail, self.calls = root, False, []
        self.by_id = {}

        def index(node):
            self.by_id[node["id"]] = node
            for c in node.get("children", []):
                c["parents"] = [node["id"]]
                index(c)
        index(root)

    def files(self):
        return self

    def list(self, q, **kw):
        return FakeList(self, q)

    def get_media(self, fileId):
        return FakeRequest(self, fileId)

    def export(self, fileId, mimeType):
        return FakeRequest(self, fileId, export=True)


@pytest.fixture(autouse=True)
def plain_download(monkeypatch):
    monkeypatch.setattr(DriveProjects, "_download", staticmethod(lambda request, limit: request.execute()))


REG = Registry.from_dict({"authors": {"Jane-Roe": ["jane-rooe"]}, "places": {"Gallery-One": []}})


def source(tmp_path, drive, **kw):
    kw = {"folder": "Projects", "categories": ("Main", "Performances"), **kw}
    return DriveProjects(tmp_path / "cache", REG, service=lambda: drive, **kw)


def test_lists_the_two_categories_with_the_category_level_dropped(tmp_path):
    src = source(tmp_path, FakeDrive(tree()))
    assert [p.name for p in src.list_projects()] == ["Embroidery", "Sea"]


def test_project_reads_images_texts_and_names_by_the_registry(tmp_path):
    src = source(tmp_path, FakeDrive(tree()))
    sea = next(p for p in src.list_projects() if p.name == "Sea")
    by_id = {i.id: i for i in sea.images}
    assert [i.id for i in sea.images] == ["i1", "i2", "i3"]          # root first, natural order, then subfolders
    assert (by_id["i1"].role, by_id["i1"].author, by_id["i1"].place, by_id["i1"].year) == ("exhibition", "Jane Roe", "Gallery One", "2024")
    assert (by_id["i2"].width, by_id["i2"].height) == (300, 400)      # Drive's rotation applied
    assert (by_id["i3"].kind, by_id["i3"].group, by_id["i3"].place) == ("png", "2024, Exposition in Northbay", "Northbay")
    assert sea.descriptions == {"en": "The sea project.", "uk": CYRILLIC}
    assert [u.reason for u in sea.unsupported] == ["video, not an image"]


def test_reads_come_from_the_cache_until_refresh_is_asked(tmp_path):
    drive = FakeDrive(tree())
    src = source(tmp_path, drive)
    src.list_projects()
    drive.calls.clear()
    again = source(tmp_path, drive)                  # a new process: the cache file, no Drive
    assert [p.name for p in again.list_projects()] == ["Embroidery", "Sea"]
    pid = again.list_projects()[1].id
    assert again.fingerprint(pid, "i1") == "i1-v1"
    assert drive.calls == []

    drive.by_id["i1"]["md5Checksum"] = "v2"
    drive.by_id["p2"]["children"].append(file("i5", "b.jpg", "image/jpeg", JPEG, parents=["p2"]))
    drive.by_id["i5"] = drive.by_id["p2"]["children"][-1]
    assert again.fingerprint(pid, "i1") == "i1-v1"    # not seen before a refresh
    again.refresh()
    assert again.fingerprint(pid, "i1") == "i1-v2"
    assert len(again.get_project(again.list_projects()[0].id).images) == 2


def test_a_second_instance_sees_a_refresh_made_by_another(tmp_path):
    # The API's worker process (web/jobs.py) has its own instance over the same cache file.
    drive = FakeDrive(tree())
    main, worker = source(tmp_path, drive), source(tmp_path, drive)
    pid = main.list_projects()[1].id
    assert worker.fingerprint(pid, "i1") == "i1-v1"
    drive.by_id["i1"]["md5Checksum"] = "v2"
    drive.calls.clear()
    main.refresh()
    assert worker.fingerprint(pid, "i1") == "i1-v2"
    drive.calls.clear()
    assert worker.list_projects() and drive.calls == []            # read from the file, not from Drive


def test_refresh_downloads_only_changed_texts(tmp_path):
    drive = FakeDrive(tree())
    src = source(tmp_path, drive)
    src.refresh()
    drive.calls.clear()
    src.refresh()
    assert [c for c in drive.calls if c[0] == "media"] == []      # texts and sniffed types are cached by version
    drive.by_id["t1"].update(md5Checksum="v2", data=docx("New text."))
    drive.calls.clear()
    src.refresh()
    assert [c[1] for c in drive.calls if c[0] == "media" and c[1] in ("t1", "g1")] == ["t1"]
    assert src.list_projects()[1].descriptions["en"] == "New text."


def test_read_image_fetches_the_original_and_never_stores_it(tmp_path):
    drive = FakeDrive(tree())
    src = source(tmp_path, drive)
    pid = src.list_projects()[1].id
    assert src.read_image(pid, "i1") == JPEG
    stored = b"".join(p.read_bytes() for p in (tmp_path / "cache").rglob("*") if p.is_file())
    assert JPEG not in stored
    with pytest.raises(NotFound):
        src.read_image(pid, "t1")                    # a text is not an image
    with pytest.raises(NotFound):
        src.read_image("nope", "i1")


def test_failed_refresh_keeps_the_listing_and_says_as_of_when(tmp_path):
    drive = FakeDrive(tree())
    ticks = iter(range(1_700_000_000, 1_700_000_100))
    src = source(tmp_path, drive, clock=lambda: float(next(ticks)))
    src.refresh()
    drive.fail = True
    with pytest.raises(SourceError, match=r"Drive could not be read \(ConnectionError\)\. Showing the projects as of 2023-11-1"):
        src.refresh()
    assert [p.name for p in src.list_projects()] == ["Embroidery", "Sea"]


def test_first_use_without_drive_fails_loudly(tmp_path):
    drive = FakeDrive(tree())
    drive.fail = True
    with pytest.raises(SourceError, match="Drive could not be read"):
        source(tmp_path, drive).list_projects()


def test_missing_category_is_an_error_not_an_empty_list(tmp_path):
    t = tree()
    t["children"] = t["children"][:1]
    with pytest.raises(SourceError, match="Performances"):
        source(tmp_path, FakeDrive(t)).list_projects()


def test_cache_holds_no_originals_and_is_private(tmp_path):
    src = source(tmp_path, FakeDrive(tree()))
    src.refresh()
    state = tmp_path / "cache" / "state.json"
    assert oct(state.stat().st_mode & 0o777) == "0o600"
    assert "data" not in json.dumps(json.loads(state.read_text("utf-8"))["projects"][0]["files"][0])


# --- HTTP and config ----------------------------------------------------------

def test_refresh_endpoint_and_thumbnails_over_http(tmp_path):
    import threading
    import urllib.error
    import urllib.request

    from PIL import Image

    from core.config import Config
    from core.db import Database
    from web import server as s

    buf = io.BytesIO()
    Image.new("RGB", (40, 30), "red").save(buf, "JPEG")
    t = tree()
    drive = FakeDrive(t)
    drive.by_id["i1"]["data"] = buf.getvalue()
    cfg = Config(updates="fixtures", projects="fixtures", cv="fixtures", llm="mock", port=0, data_dir=tmp_path / "data")
    db = Database(cfg.db_path)
    db.migrate()
    server = s.make_server(cfg, db, port=0, projects=source(tmp_path, drive))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    def call(method, path):
        req = urllib.request.Request(base + path, method=method, data=b"{}" if method == "POST" else None,
                                     headers={"Content-Type": "application/json"})
        if not req.has_header("Origin"):   # a browser sends it on every write
            req.add_header("Origin", "http://" + req.host)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    try:
        status, body = call("POST", "/api/projects/refresh")
        assert status == 200
        items = json.loads(body)["items"]
        assert [i["name"] for i in items] == ["Embroidery", "Sea"]
        status, body = call("GET", f"/api/thumbs/{items[1]['id']}/i1")
        assert status == 200 and body[:3] == b"\xff\xd8\xff"
        drive.fail = True
        status, body = call("POST", "/api/projects/refresh")
        assert status == 502 and "Showing the projects as of" in json.loads(body)["error"]["message"]
        assert call("GET", "/api/projects")[0] == 200           # the last listing still serves
    finally:
        server.shutdown()
        server.server_close()


def test_without_categories_the_project_folders_sit_in_the_shared_folder(tmp_path):
    t = folder("root", "Portfolio", [
        folder("p1", "Sea", [file("i1", "a.jpg", "image/jpeg", JPEG)]),
        folder("p2", "Embroidery", [file("i2", "b.jpg", "image/jpeg", JPEG)]),
        file("x1", "notes.docx", dp.DOCX, docx("not a project")),
    ])
    names = [p.name for p in source(tmp_path, FakeDrive(t), folder="Portfolio", categories=()).list_projects()]
    assert names == ["Embroidery", "Sea"]


def test_the_folder_name_cannot_break_out_of_the_drive_query(tmp_path):
    drive = FakeDrive(tree())
    source(tmp_path, drive, folder="Jane's \\ work' or name != '").refresh()
    query = next(q for kind, q in drive.calls if kind == "list" and q != "trashed=false")
    assert query.startswith("name='Jane\\'s \\\\ work\\' or name != \\'' and mimeType=")


def test_a_shared_folder_name_is_required():
    with pytest.raises(ValueError, match="shared folder"):
        DriveProjects(Path("/tmp/x"), REG, service=lambda: None, folder=" ")


def test_config_projects_setting():
    from core.config import ConfigError, example_data, parse

    def conf(**projects):
        data = example_data()
        data["server"]["data_dir"] = "/var/lib/arta-test"
        data["projects"].update(projects)
        data["google"]["token_file"] = "/srv/token.json"
        return data
    assert parse(conf(source="none")).projects is None
    cfg = parse(conf(source="drive", drive_folder="Projects", drive_categories=["Main"]))
    assert cfg.projects == "drive" and str(cfg.drive_projects_dir) == "/var/lib/arta-test/cache/drive-projects"
    with pytest.raises(ConfigError, match="projects.source"):
        parse(conf(source="dropbox"))


def test_build_projects_picks_drive(tmp_path):
    from core.config import Config
    from web import server as s
    cfg = Config(llm="mock", port=0, data_dir=tmp_path, projects="drive", drive_folder="Projects",
                 drive_categories=("Main",))
    built = s.build_projects(cfg)
    assert isinstance(built, DriveProjects) and (built.folder, built.categories) == ("Projects", ("Main",))
    assert type(s.build_projects(Config(updates="fixtures", projects="fixtures", cv="fixtures", llm="mock", port=0, data_dir=tmp_path))).__name__ == "LocalProjects"
