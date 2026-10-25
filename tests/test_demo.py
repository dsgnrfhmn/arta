"""`python -m demo`: made-up data only, drawn the same way every time, and every page of the
dashboard has something to show on it."""

import io
import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from core.config import REPO_ROOT, ConfigError
from core.db import Database
from demo import __main__ as demo
from demo import paint
from demo.build import build_projects, pixels, work_file
from sources import project_files as pf
from sources.local_projects import LocalProjects
from web import server as s

SPEC = json.loads((REPO_ROOT / "demo" / "projects.json").read_text("utf-8"))


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    """The demo's data folder after a first start: pictures drawn, database filled."""
    data_dir = tmp_path_factory.mktemp("demo-data")
    cfg = demo.demo_config(data_dir, None)
    demo.prepare(data_dir, reset=False)
    return cfg, demo.fill(cfg)


# --- the configuration -------------------------------------------------------------------

def test_the_demo_uses_only_demo_sources_and_the_mock(built):
    cfg, _ = built
    assert cfg.intake == ("demo",) and (cfg.updates, cfg.projects, cfg.cv) == ("demo", "demo", "demo")
    assert cfg.llm == "mock" and not cfg.uses_google and cfg.api_key_file is None and cfg.port == 8455


def test_a_demo_config_that_reaches_anything_real_is_refused(tmp_path, monkeypatch):
    text = (REPO_ROOT / "demo" / "config.toml").read_text("utf-8")
    (tmp_path / "config.toml").write_text(text.replace('sources = ["demo"]', 'sources = ["artconnect"]'), "utf-8")
    monkeypatch.setattr(demo, "DEMO_DIR", tmp_path)
    with pytest.raises(ConfigError, match="only the demo sources"):
        demo.demo_config(tmp_path / "data", None)


def test_the_data_folder_is_outside_the_repository():
    with pytest.raises(ConfigError, match="inside the repository"):
        demo.demo_config(REPO_ROOT / "demo-data", None)


def test_default_data_folder_per_platform():
    env = {"HOME": "/Users/x"}
    assert demo.default_data_dir(env, "darwin") == Path("/Users/x/Library/Application Support/arta-demo")
    assert demo.default_data_dir({"HOME": "/home/x"}, "linux") == Path("/home/x/.local/share/arta-demo")
    assert demo.default_data_dir({"HOME": "/home/x", "XDG_DATA_HOME": "/d"}, "linux") == Path("/d/arta-demo")


# --- the data folder: marked, kept, reset ------------------------------------------------

def test_a_folder_that_is_not_the_demos_is_never_touched(tmp_path):
    (tmp_path / "notes.txt").write_text("mine")
    with pytest.raises(ConfigError, match="not a demo folder"):
        demo.prepare(tmp_path, reset=True)
    assert (tmp_path / "notes.txt").read_text() == "mine"


def test_reset_deletes_the_marked_folder_and_starts_again(tmp_path):
    folder = tmp_path / "demo"
    demo.prepare(folder, reset=False)
    (folder / "edited.sqlite3").write_text("x")
    demo.prepare(folder, reset=False)
    assert (folder / "edited.sqlite3").exists()                       # kept between starts
    demo.prepare(folder, reset=True)
    assert not (folder / "edited.sqlite3").exists() and (folder / demo.MARK).is_file()
    assert oct(folder.stat().st_mode & 0o777) == "0o700"


def test_a_second_start_keeps_what_the_user_changed(built):
    cfg, first = built
    assert first == {"pictures": 39, "opportunities": 24, "updates": 3, "cv_sets": 3, "portfolio_sets": 4}
    assert demo.fill(cfg) == {"pictures": 0, "opportunities": 0, "updates": 0, "cv_sets": 0, "portfolio_sets": 0}


# --- the drawn projects ------------------------------------------------------------------

def test_projects_are_read_like_a_real_project_folder(built):
    cfg, _ = built
    projects = {p.name: p for p in LocalProjects(cfg.demo_projects_dir).list_projects()}
    assert sorted(projects) == sorted(p["name"] for p in SPEC["projects"])
    for spec in SPEC["projects"]:
        p = projects[spec["name"]]
        shows = sum(len(e["views"]) + len(e["details"]) for e in spec["exhibitions"])
        assert sum(i.role == "work" for i in p.images) == len(spec["works"])
        assert sum(i.role == "exhibition" for i in p.images) == shows
        assert p.descriptions["en"].startswith(spec["description"][0][:40])


def test_pictures_are_the_same_on_every_build(built):
    cfg, _ = built
    spec = SPEC["projects"][0]
    work = spec["works"][2]
    again = io.BytesIO()
    paint.work(spec["style"], spec["seed"] + 2, pixels(work["size"])).save(again, "JPEG", quality=86)
    assert (cfg.demo_projects_dir / spec["name"] / work_file(work, SPEC["artist"])).read_bytes() == again.getvalue()


def test_a_work_keeps_its_proportions():
    assert pixels([120, 90]) == (1400, 1050) and pixels([30, 40]) == (1050, 1400) and pixels([80, 80]) == (1400, 1400)


def test_a_build_never_overwrites_and_never_leaves_half_a_folder(tmp_path):
    (tmp_path / "out").mkdir()
    with pytest.raises(FileExistsError):
        build_projects(REPO_ROOT / "demo" / "projects.json", tmp_path / "out")
    assert not (tmp_path / "out.partial").exists()


def test_the_description_is_a_readable_docx(built):
    cfg, _ = built
    docx = cfg.demo_projects_dir / "Rooms" / "Rooms.docx"
    assert pf.docx_text(docx).startswith("Rooms is a series of small paintings")


# --- every page has something to show -----------------------------------------------------

@pytest.fixture(scope="module")
def srv(built):
    cfg, _ = built
    server = s.make_server(cfg, Database(cfg.db_path), port=0, **s.load_runtime(cfg))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()
    server.server_close()


def call(srv, method, path, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{srv.server_address[1]}{path}", method=method, data=data,
                                 headers={"Content-Type": "application/json"} if data else {})
    req.add_header("Origin", "http://" + req.host)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            return r.status, json.loads(raw) if r.headers.get_content_type() == "application/json" else raw
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_for_you_shows_every_state(srv):
    status, body = call(srv, "GET", "/api/opportunities")
    assert status == 200 and len(body["items"]) == 24
    stages = {i["stage"] for i in body["items"]}
    assert {"for_review", "analysed", "filtered"} <= stages
    assert call(srv, "GET", "/api/health")[1]["unread"] == 3


def test_load_new_opportunities_reads_the_demo_mailbox_and_filters(srv):
    status, report = call(srv, "POST", "/api/intake/run", {})
    assert status == 200 and report["added"] == 2 and report["filtered"] == 1


def test_portfolio_sets_have_pictures_and_text(srv):
    _, sets = call(srv, "GET", "/api/sets/portfolio")
    sid = next(i["id"] for i in sets["items"] if i["name"] == "Coastal Residency application")
    status, preview = call(srv, "GET", f"/api/sets/portfolio/{sid}/preview")
    pages = preview["pages"]
    assert status == 200 and any(pg["images"] for pg in pages) and any(pg["paragraphs"] for pg in pages)
    page = next(pg for pg in pages if pg["images"])
    image = page["images"][0]
    assert image["caption"]["label"].startswith("“") and image["caption"]["lines"] == ["© Jane Roe"]
    status, jpeg = call(srv, "GET", f"/api/thumbs/{page['project_id']}/{image['id']}")
    assert status == 200 and jpeg[:3] == b"\xff\xd8\xff"


def test_cv_sets_have_blocks(srv):
    _, sets = call(srv, "GET", "/api/sets/cv")
    assert sorted(i["name"] for i in sets["items"]) == [
        "Full CV for the Riverside Foundation", "Short CV: residencies", "Textile CV"]
    short = next(i for i in sets["items"] if i["name"] == "Short CV: residencies")
    assert short["entry_count"] == 14 and len(call(srv, "GET", f"/api/sets/cv/{short['id']}")[1]["entries"]) == 14


def test_a_set_can_start_with_one_work_beside_its_text(srv):
    _, sets = call(srv, "GET", "/api/sets/portfolio")
    sid = next(i["id"] for i in sets["items"] if i["name"] == "Textile Art Prize")
    opening = call(srv, "GET", f"/api/sets/portfolio/{sid}/preview")[1]["pages"][0]
    assert opening["layout"] == "side" and len(opening["images"]) == 1 and opening["paragraphs"]
