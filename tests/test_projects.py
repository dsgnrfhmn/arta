"""ProjectSource (fixtures), thumbnail cache, and the project endpoints.

Fixture tree: tests/fixtures/projects (one folder per project, e.g. Harbour). Synthetic trees cover what the real one
does not: symlinks, names that lie, missing text, duplicate languages."""

import hashlib
import io
import json
import threading
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

import pytest
from PIL import Image

from core.config import Config
from core.db import Database
from models.project import IMAGE_KINDS
from sources import project_files as pf
from sources import thumb_cache
from sources.local_projects import LocalProjects
from sources.project_source import NotFound, SourceError
from sources.thumb_cache import ThumbCache, ThumbError
from web import server as s

# The synthetic fixture tree: the layout of the real Drive folders (groups, exposition
# folders, a docx per language, an extension-less audio file), made-up content.
REAL = Path(__file__).resolve().parent / "fixtures" / "projects"


def jpeg(w=2000, h=1000, color="red"):
    b = io.BytesIO()
    Image.new("RGB", (w, h), color).save(b, "JPEG")
    return b.getvalue()


def png():
    b = io.BytesIO()
    Image.new("RGB", (10, 10)).save(b, "PNG")
    return b.getvalue()


def docx(*paragraphs):
    ns = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    xml = f'<?xml version="1.0"?><w:document {ns}><w:body>{body}</w:body></w:document>'
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("word/document.xml", xml)
    return b.getvalue()


@pytest.fixture
def tree(tmp_path):
    root = tmp_path / "projects"
    (root / "Alpha" / "2024_Show-Paris").mkdir(parents=True)
    (root / "Alpha" / "a10.jpg").write_bytes(jpeg())
    (root / "Alpha" / "a2.jpg").write_bytes(jpeg())
    (root / "Alpha" / "2024_Show-Paris" / "s1.png").write_bytes(png())
    (root / "Alpha" / "notes.docx").write_bytes(docx("Hello there", "Second paragraph"))
    return root


# --- what a file is ---------------------------------------------------------

def real_project(prefix):
    return next(p for p in LocalProjects(REAL).list_projects() if p.name.startswith(prefix))


def test_real_project_is_read_as_it_is():
    p = real_project("Harbour")
    assert len(p.images) == 19 and {i.kind for i in p.images} <= set(IMAGE_KINDS)
    assert {i.group for i in p.images} == {None, "2023, Exposition in Eastmoor", "2024, Exposition in Northbay"}
    assert sorted(p.descriptions) == ["en", "uk"]
    # the extension-less file is audio, not an image, and it is not hidden
    bad = next(u for u in p.unsupported if "audio" in u.reason)
    assert bad.name.startswith("“Harbour” by ")
    # exhibition photos are told apart by name prefix and folder, not by depth
    assert {i.role for i in p.images if i.group and "Exposition" in i.group} == {"exhibition"}
    assert any(i.role == "work" for i in p.images)


def test_type_comes_from_content_not_name(tree):
    (tree / "Alpha" / "really-a-jpeg.txt").write_bytes(jpeg())
    (tree / "Alpha" / "fake.jpg").write_bytes(b"just text, not an image")
    (p,) = LocalProjects(tree).list_projects()
    assert "really-a-jpeg.txt" in {i.name for i in p.images}
    assert "fake.jpg" not in {i.name for i in p.images}
    assert "fake.jpg" in {u.name for u in p.unsupported}


def test_order_is_root_first_then_subfolders_natural(tree):
    (p,) = LocalProjects(tree).list_projects()
    assert [(i.group, i.name) for i in p.images] == [
        (None, "a2.jpg"), (None, "a10.jpg"), ("2024_Show-Paris", "s1.png")]


def test_missing_parts_are_normal(tmp_path):
    (tmp_path / "Empty").mkdir()
    (tmp_path / "OnlyText").mkdir()
    (tmp_path / "OnlyText" / "t.docx").write_bytes(docx("Only English text here"))
    a, b = LocalProjects(tmp_path).list_projects()
    assert (a.name, a.images, a.descriptions) == ("Empty", (), {})
    assert b.images == () and list(b.descriptions) == ["en"]


def test_two_texts_in_one_language_keep_the_first_and_say_so(tree):
    (tree / "Alpha" / "notes2.docx").write_bytes(docx("Another English text"))
    (p,) = LocalProjects(tree).list_projects()
    assert p.descriptions["en"].startswith("Hello there")
    assert any("other en text" in u.reason for u in p.unsupported)


def test_text_in_a_subfolder_is_used_when_the_root_has_none(tmp_path):
    root = tmp_path / "projects"
    deep = root / "Sea" / "Part 1" / "Paintings"
    deep.mkdir(parents=True)
    (deep / "w.jpg").write_bytes(jpeg())
    (root / "Sea" / "Part 1" / "Part one.docx").write_bytes(docx("Part one text"))
    (root / "Sea" / "Part 1" / "Paintings" / "deeper.docx").write_bytes(docx("Deeper text"))
    (p,) = LocalProjects(root).list_projects()
    assert p.descriptions["en"] == "Part one text"
    assert any("deeper.docx" == u.name and "not used" in u.reason for u in p.unsupported)
    assert [i.group for i in p.images] == ["Part 1/Paintings"]


def test_role_and_credit_come_from_the_name_and_folders(tmp_path):
    root = tmp_path / "projects"
    (root / "Sea" / "Photo of Exposition").mkdir(parents=True)
    (root / "Sea" / "Paintings").mkdir()
    (root / "Sea" / "IW-1__Jane-Roe__Gallery-One__2023.jpg").write_bytes(jpeg())
    (root / "Sea" / "Paintings" / "Work-1__John-Doe__Annex__2024.jpg").write_bytes(jpeg())
    (root / "Sea" / "Photo of Exposition" / "IMG_1__not-specified.jpg").write_bytes(jpeg())
    (root / "Sea" / "plain.jpg").write_bytes(jpeg())
    (p,) = LocalProjects(root).list_projects()
    by = {i.name: i for i in p.images}
    iw = by["IW-1__Jane-Roe__Gallery-One__2023.jpg"]
    assert (iw.role, iw.author, iw.place, iw.year) == ("exhibition", "Jane Roe", "Gallery One", "2023")
    assert (by["Work-1__John-Doe__Annex__2024.jpg"].role, by["Work-1__John-Doe__Annex__2024.jpg"].place) == ("work", "Annex")
    assert by["IMG_1__not-specified.jpg"].role == "exhibition"
    assert by["IMG_1__not-specified.jpg"].author == "not-specified"
    assert (by["plain.jpg"].role, by["plain.jpg"].author, by["plain.jpg"].year) == ("work", "not-specified", "")
    assert iw.to_api()["author"] == "Jane Roe"


def test_broken_docx_is_reported_not_fatal(tree):
    (tree / "Alpha" / "broken.docx").write_bytes(b"PK\x03\x04 not really a zip")
    (p,) = LocalProjects(tree).list_projects()
    assert len(p.images) == 3
    assert any(u.name == "broken.docx" and "not usable" in u.reason for u in p.unsupported)


def test_symlinks_are_never_followed(tree, tmp_path):
    secret = tmp_path / "secret.jpg"
    secret.write_bytes(jpeg())
    (tree / "Alpha" / "link.jpg").symlink_to(secret)
    (tree / "Alpha" / "dirlink").symlink_to(tmp_path)
    (p,) = LocalProjects(tree).list_projects()
    assert "link.jpg" not in {i.name for i in p.images}
    assert any(u.name == "link.jpg" and "symbolic link" in u.reason for u in p.unsupported)
    assert all(i.group != "dirlink" for i in p.images)


def test_hidden_files_are_skipped(tree):
    (tree / "Alpha" / ".DS_Store").write_bytes(b"x")
    (p,) = LocalProjects(tree).list_projects()
    assert all(not u.name.startswith(".") for u in p.unsupported)


def test_names_are_sanitized(tree):
    (tree / "Alpha" / "<b>bold<i>.jpg").write_bytes(jpeg())
    (p,) = LocalProjects(tree).list_projects()
    assert "bold.jpg" in {i.name for i in p.images}


def test_natural_order():
    names = ["Space-10.jpg", "space-2.jpg", "a1.jpg", "a.jpg", "a.jpg_original", "b"]
    assert sorted(names, key=pf.natural_key) == ["a.jpg", "a.jpg_original", "a1.jpg", "b", "space-2.jpg", "Space-10.jpg"]


def test_language_detection():
    assert pf.language_of("Project Harbour, oil on canvas") == "en"
    assert pf.language_of("«" + " ".join("".join(chr(0x0430 + (7 * i + j) % 32) for j in range(6)) for i in range(4)) + "», 2024") == "uk"   # Cyrillic letters, no words
    assert pf.language_of("1234 ...") == "en"


# --- ids are the only handle ------------------------------------------------

def test_ids_cannot_reach_other_files(tree):
    src = LocalProjects(tree)
    (p,) = src.list_projects()
    for bad in ("../../etc/passwd", "..", "", "notes.docx", "a2.jpg"):
        with pytest.raises(NotFound):
            src.read_image(p.id, bad)
    with pytest.raises(NotFound):
        src.read_image("../Alpha", p.images[0].id)
    with pytest.raises(NotFound):
        src.get_project("nope")


def test_a_non_image_has_no_image_id():
    src = LocalProjects(REAL)
    p = real_project("Harbour")
    assert len({i.id for i in p.images}) == len(p.images)       # unique
    bad = next(u for u in p.unsupported if "audio" in u.reason)
    fake = hashlib.sha256(f"{p.name}\0/{bad.name}".encode()).hexdigest()[:16]
    with pytest.raises(NotFound):
        src.read_image(p.id, fake)                              # the audio file's own id


# --- thumbnail cache --------------------------------------------------------

class Spy:
    """Counts reads of originals."""
    def __init__(self, inner):
        self.inner, self.reads = inner, 0

    def fingerprint(self, *a):
        return self.inner.fingerprint(*a)

    def read_image(self, *a):
        self.reads += 1
        return self.inner.read_image(*a)


def test_thumbnail_is_small_private_and_cached(tree, tmp_path):
    src = Spy(LocalProjects(tree))
    (p,) = src.inner.list_projects()
    cache = ThumbCache(tmp_path / "cache")
    first = cache.get(src, p.id, p.images[0].id)
    assert max(Image.open(first).size) <= 800
    assert oct(first.stat().st_mode & 0o777) == "0o600" and oct(cache.dir.stat().st_mode & 0o777) == "0o700"
    assert src.reads == 1
    assert cache.get(src, p.id, p.images[0].id) == first
    assert src.reads == 1                       # second load: no read of the original


def test_changed_original_refreshes_and_drops_the_stale_thumbnail(tree, tmp_path):
    src = Spy(LocalProjects(tree))
    (p,) = src.inner.list_projects()
    img = p.images[0]
    cache = ThumbCache(tmp_path / "cache")
    old = cache.get(src, p.id, img.id)
    (tree / "Alpha" / img.name).write_bytes(jpeg(3000, 3000, "blue"))
    new = cache.get(src, p.id, img.id)
    assert new != old and not old.exists() and src.reads == 2
    assert Image.open(new).getpixel((5, 5))[2] > 200          # the new (blue) image


def test_orientation_is_applied(tmp_path):
    root = tmp_path / "p" / "A"
    root.mkdir(parents=True)
    exif = Image.Exif()
    exif[0x0112] = 6                                            # rotate 90 on display
    b = io.BytesIO()
    Image.new("RGB", (400, 200), "green").save(b, "JPEG", exif=exif)
    (root / "r.jpg").write_bytes(b.getvalue())
    src = LocalProjects(tmp_path / "p")
    (p,) = src.list_projects()
    t = ThumbCache(tmp_path / "c").get(src, p.id, p.images[0].id)
    assert Image.open(t).size == (200, 400)


def test_undecodable_image_says_why(tree, tmp_path):
    (tree / "Alpha" / "broken.jpg").write_bytes(b"\xff\xd8\xff" + b"garbage" * 20)
    src = LocalProjects(tree)
    (p,) = src.list_projects()
    broken = next(i for i in p.images if i.name == "broken.jpg")
    with pytest.raises(ThumbError):
        ThumbCache(tmp_path / "c").get(src, p.id, broken.id)


@pytest.mark.skipif(thumb_cache.HEIC_SUPPORTED, reason="needs an environment without pillow-heif")
def test_heic_without_decoder_fails_loud(tmp_path):
    src = LocalProjects(REAL)
    p = next(p for p in src.list_projects() if any(i.kind == "heic" for i in p.images))
    heic = next(i for i in p.images if i.kind == "heic")
    with pytest.raises(ThumbError, match="pillow-heif"):
        ThumbCache(tmp_path / "c").get(src, p.id, heic.id)


@pytest.mark.skipif(not thumb_cache.HEIC_SUPPORTED, reason="needs pillow-heif (requirements.txt)")
def test_heic_is_converted(tmp_path):
    src = LocalProjects(REAL)
    p = next(p for p in src.list_projects() if any(i.kind == "heic" for i in p.images))
    heic = next(i for i in p.images if i.kind == "heic")
    t = ThumbCache(tmp_path / "c").get(src, p.id, heic.id)
    assert max(Image.open(t).size) <= 800


# --- HTTP -------------------------------------------------------------------

@pytest.fixture
def srv(tree, tmp_path):
    cfg = Config(updates="fixtures", projects="fixtures", cv="fixtures", llm="mock", port=0, data_dir=tmp_path / "data")
    db = Database(cfg.db_path)
    db.migrate()
    server = s.make_server(cfg, db, port=0, projects=LocalProjects(tree))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()
    server.server_close()


def get(srv, path, headers=None):
    req = urllib.request.Request(f"http://127.0.0.1:{srv.server_address[1]}{path}", headers=headers or {})
    if not req.has_header("Origin"):   # a browser sends it on every write
        req.add_header("Origin", "http://" + req.host)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.read(), r.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers


def test_project_list_and_detail(srv):
    status, body, _ = get(srv, "/api/projects")
    (item,) = json.loads(body)["items"]
    assert status == 200 and item["name"] == "Alpha" and item["image_count"] == 3 and item["languages"] == ["en"]
    detail = json.loads(get(srv, "/api/projects/" + item["id"])[1])
    assert [i["name"] for i in detail["images"]] == ["a2.jpg", "a10.jpg", "s1.png"]
    assert detail["descriptions"]["en"] == "Hello there\nSecond paragraph"
    assert get(srv, "/api/projects/nope")[0] == 404


def test_thumbnail_endpoint_serves_cached_bytes_and_revalidates(srv):
    pid = json.loads(get(srv, "/api/projects")[1])["items"][0]["id"]
    iid = json.loads(get(srv, "/api/projects/" + pid)[1])["images"][0]["id"]
    status, data, h = get(srv, f"/api/thumbs/{pid}/{iid}")
    assert status == 200 and h["Content-Type"] == "image/jpeg" and h["X-Content-Type-Options"] == "nosniff"
    assert h["Cache-Control"] == "private, no-cache" and max(Image.open(io.BytesIO(data)).size) <= 800
    status, data, _ = get(srv, f"/api/thumbs/{pid}/{iid}", {"If-None-Match": h["ETag"]})
    assert status == 304 and data == b""


def test_thumbnail_endpoint_refuses_unknown_and_traversal_ids(srv):
    pid = json.loads(get(srv, "/api/projects")[1])["items"][0]["id"]
    for path in (f"/api/thumbs/{pid}/nope", "/api/thumbs/nope/nope", f"/api/thumbs/{pid}/..",
                 "/api/thumbs/..%2f..%2fetc/passwd"):
        assert get(srv, path)[0] == 404, path


def test_undecodable_thumbnail_is_422_not_500(srv, tree):
    (tree / "Alpha" / "broken.jpg").write_bytes(b"\xff\xd8\xff" + b"garbage" * 20)
    pid = json.loads(get(srv, "/api/projects")[1])["items"][0]["id"]
    iid = next(i["id"] for i in json.loads(get(srv, "/api/projects/" + pid)[1])["images"] if i["name"] == "broken.jpg")
    status, body, _ = get(srv, f"/api/thumbs/{pid}/{iid}")
    assert status == 422 and json.loads(body)["error"]["code"] == "unprocessable"


# --- image_size: the header's size, as the picture is shown -----------------

def test_image_size_reads_png_jpeg_webp(tmp_path):
    for kind, ext, fmt in (("png", "png", "PNG"), ("jpeg", "jpg", "JPEG"), ("webp", "webp", "WEBP")):
        path = tmp_path / f"a.{ext}"
        Image.new("RGB", (120, 80), "red").save(path, fmt)
        assert pf.image_size(path, kind) == (120, 80), kind


def test_image_size_applies_exif_orientation(tmp_path):
    path = tmp_path / "turned.jpg"
    im = Image.new("RGB", (120, 80), "red")
    exif = im.getexif()
    exif[0x0112] = 6                     # rotate 90: shown portrait
    im.save(path, "JPEG", exif=exif)
    assert pf.image_size(path, "jpeg") == (80, 120)


def test_image_size_of_a_broken_file_is_unknown_not_an_error(tmp_path):
    path = tmp_path / "x.jpg"
    path.write_bytes(b"\xff\xd8\xff\xe0 not really")
    assert pf.image_size(path, "jpeg") == (0, 0)
    assert pf.image_size(tmp_path / "missing.png", "png") == (0, 0)
    assert pf.image_size(path, "heic") == (0, 0)


def test_image_size_of_a_synthetic_heic_box_layout(tmp_path):
    ispe = b"\x00\x00\x00\x14ispe" + b"\x00\x00\x00\x00" + (4032).to_bytes(4, "big") + (3024).to_bytes(4, "big")
    irot = b"\x00\x00\x00\x09irot\x01"
    path = tmp_path / "p.heic"
    path.write_bytes(b"\x00\x00\x00\x18ftypheic" + b"\x00" * 12 + ispe + irot)
    assert pf.image_size(path, "heic") == (3024, 4032)      # a quarter turn: shown portrait


def test_project_images_carry_their_size(tmp_path):
    folder = tmp_path / "Proj"
    folder.mkdir()
    Image.new("RGB", (300, 200), "red").save(folder / "A-1__Jane-Roe.jpg", "JPEG")
    (project,) = LocalProjects(tmp_path).list_projects()
    image = project.images[0]
    assert (image.width, image.height) == (300, 200)
    assert image.to_api()["width"] == 300 and image.to_api()["height"] == 200


def test_a_missing_project_folder_is_an_error_not_an_empty_portfolio(tmp_path):
    src = LocalProjects(tmp_path / "nope")
    with pytest.raises(SourceError, match="does not exist"):
        src.list_projects()


def test_build_projects_reads_a_local_folder(tmp_path):
    from core.config import Config
    from web import server as s
    built = s.build_projects(Config(llm="mock", port=0, data_dir=tmp_path / "d", projects="local",
                                    projects_local_dir=tmp_path))
    assert isinstance(built, LocalProjects) and built.root == tmp_path
