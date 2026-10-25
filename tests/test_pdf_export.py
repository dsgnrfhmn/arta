"""PDF export: the export registry and its routes (web/exports.py), the print server and
the rendering in Chromium (web/pdf_export.py)."""

import json
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

import pytest

from core.config import Config
from core.db import Database
from core.seed import seed_cv_sets, seed_opportunities, seed_portfolio_sets
from sources.local_cv import LocalCv
from sources.local_projects import LocalProjects
from web import pdf_export
from web import server as s
from web.exports import Exports, filename, write_progress

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"


@pytest.fixture
def cfg(tmp_path):
    return Config(updates="fixtures", projects="fixtures", cv="fixtures", llm="mock", port=0, data_dir=tmp_path / "data")


@pytest.fixture
def db(cfg):
    db = Database(cfg.db_path)
    db.migrate()
    seed_opportunities(db, FIXTURES)
    seed_portfolio_sets(db, FIXTURES, LocalProjects(FIXTURES / "projects"))
    seed_cv_sets(db, FIXTURES, LocalCv(FIXTURES / "cv" / "master.json"))
    return db


def set_id(db, kind, name):
    with db.read() as c:
        return c.execute("SELECT id FROM sets WHERE kind = ? AND name = ?", (kind, name)).fetchone()[0]


def any_set(db, kind):
    with db.read() as c:
        return c.execute("SELECT id, name FROM sets WHERE kind = ? ORDER BY position LIMIT 1", (kind,)).fetchone()


def call(port, method, path, body=None, headers=None):
    data = None if body is None else json.dumps(body).encode()
    hdrs = {**({"Content-Type": "application/json"} if data else {}), **(headers or {})}
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method, data=data, headers=hdrs)
    if not req.has_header("Origin"):   # a browser sends it on every write
        req.add_header("Origin", "http://" + req.host)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read(), r.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers


def serve(server):
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server.server_address[1]


# --- registry and routes ------------------------------------------------------

def test_filename_keeps_the_name_and_drops_path_and_control_characters():
    assert filename("Wexford application") == "Wexford application.pdf"
    assert filename('a/b\\c:d*?"<>|\x00e') == "a b c d e.pdf"
    assert filename("...") == "set.pdf"
    assert filename("Harbour’s edge") == "Harbour’s edge.pdf"
    cyrillic = "".join(chr(0x0430 + i) for i in range(8))          # letters only, no words
    assert filename(cyrillic) == cyrillic + ".pdf"


@pytest.fixture
def pool():
    with ThreadPoolExecutor(max_workers=2) as p:
        yield p


def test_start_poll_and_download_over_http(cfg, db, pool):
    gate = threading.Event()

    def fake_render(kind, set_id, path):
        write_progress(Path(path), {"phase": "pictures", "done": 2, "total": 5})
        gate.wait(5)
        Path(path).write_bytes(b"%PDF-1.4 " + f"{kind}:{set_id}".encode())
        return 3

    exports = Exports(cfg.exports_dir, lambda k, i, p: pool.submit(fake_render, k, i, p))
    port = serve(s.make_server(cfg, db, port=0, exports=exports))
    sid, name = any_set(db, "portfolio")
    status, body, _ = call(port, "POST", f"/api/sets/portfolio/{sid}/pdf", {})
    job = json.loads(body)
    assert status == 202 and job["status"] == "running"
    for _ in range(50):     # the renderer's progress is in the state as soon as it reports
        state = json.loads(call(port, "GET", f"/api/exports/{job['job']}")[1])
        if "phase" in state:
            break
        time.sleep(0.05)
    assert state == {"status": "running", "phase": "pictures", "done": 2, "total": 5}
    assert call(port, "GET", f"/api/exports/{job['job']}/file")[0] == 409
    gate.set()
    for _ in range(50):
        state = json.loads(call(port, "GET", f"/api/exports/{job['job']}")[1])
        if state["status"] != "running":
            break
        time.sleep(0.05)
    assert state == {"status": "done", "pages": 3}
    status, data, headers = call(port, "GET", f"/api/exports/{job['job']}/file")
    assert status == 200 and data == f"%PDF-1.4 portfolio:{sid}".encode()
    assert headers["Content-Type"] == "application/pdf"
    assert f'filename="{name}.pdf"'.encode("ascii", "replace").decode() in headers["Content-Disposition"]
    assert cfg.exports_dir.stat().st_mode & 0o077 == 0
    assert not list(cfg.exports_dir.glob("*.progress.json"))      # gone once the export is done


def test_exports_belong_to_the_dashboard_not_to_a_page(cfg, db, pool):
    gate = threading.Event()

    def fake_render(kind, set_id, path):
        if set_id != "fails":
            gate.wait(5)
        if kind == "cv":
            raise pdf_export.ExportError("No pages.")
        Path(path).write_bytes(b"%PDF-1.4")
        return 2

    exports = Exports(cfg.exports_dir, lambda k, i, p: pool.submit(fake_render, k, i, p))
    port = serve(s.make_server(cfg, db, port=0, exports=exports))
    sid, name = any_set(db, "portfolio")
    first = json.loads(call(port, "POST", f"/api/sets/portfolio/{sid}/pdf", {})[1])["job"]
    again = json.loads(call(port, "POST", f"/api/sets/portfolio/{sid}/pdf", {})[1])["job"]
    assert again == first                                   # a page that came back follows it
    items = json.loads(call(port, "GET", "/api/exports")[1])["items"]
    assert [(i["job"], i["kind"], i["set_id"], i["name"], i["status"]) for i in items] == [(first, "portfolio", sid, name, "running")]
    gate.set()
    time.sleep(0.2)
    assert json.loads(call(port, "GET", "/api/exports")[1])["items"][0]["status"] == "done"   # offered until fetched
    assert call(port, "GET", f"/api/exports/{first}/file")[0] == 200
    assert json.loads(call(port, "GET", "/api/exports")[1])["items"] == []
    cv_sid, _ = any_set(db, "cv")
    failed = json.loads(call(port, "POST", f"/api/sets/cv/{cv_sid}/pdf", {})[1])["job"]
    time.sleep(0.2)
    told = json.loads(call(port, "GET", "/api/exports")[1])["items"]
    assert [(i["job"], i["status"], i["message"]) for i in told] == [(failed, "error", "No pages.")]
    assert json.loads(call(port, "GET", "/api/exports")[1])["items"] == []    # told once
    again = json.loads(call(port, "POST", f"/api/sets/portfolio/{sid}/pdf", {})[1])["job"]
    assert again != first                                   # a finished export is not reused


def test_stop_ends_a_running_export_and_takes_a_waiting_one_off_the_queue(cfg, db):
    from web.exports import stop_path

    def fake_render(kind, set_id, path):
        for _ in range(200):                 # the renderer asks stopped() while it works
            if stop_path(Path(path)).exists():
                raise pdf_export.ExportStopped("Stopped.")
            time.sleep(0.02)
        Path(path).write_bytes(b"%PDF-1.4")
        return 1

    with ThreadPoolExecutor(max_workers=1) as one:          # one worker, as in production
        exports = Exports(cfg.exports_dir, lambda k, i, p: one.submit(fake_render, k, i, p))
        port = serve(s.make_server(cfg, db, port=0, exports=exports))
        pf, _ = any_set(db, "portfolio")
        cv, _ = any_set(db, "cv")
        running = json.loads(call(port, "POST", f"/api/sets/portfolio/{pf}/pdf", {})[1])["job"]
        waiting = json.loads(call(port, "POST", f"/api/sets/cv/{cv}/pdf", {})[1])["job"]
        time.sleep(0.1)
        assert json.loads(call(port, "DELETE", f"/api/exports/{waiting}")[1]) == {"status": "stopped"}
        assert json.loads(call(port, "DELETE", f"/api/exports/{running}")[1]) == {"status": "running", "phase": "stopping"}
        time.sleep(0.3)
        assert json.loads(call(port, "GET", f"/api/exports/{running}")[1]) == {"status": "stopped"}
        assert json.loads(call(port, "GET", "/api/exports")[1])["items"] == []         # not offered
        assert not list(cfg.exports_dir.glob("*"))                                    # nothing left behind
        assert call(port, "DELETE", "/api/exports/nope")[0] == 404


def test_errors_reach_the_page_as_a_message(cfg, db, pool):
    def broken(kind, set_id, path):
        raise pdf_export.ExportError("A picture could not be loaded from Drive.")

    def crashed(kind, set_id, path):
        raise RuntimeError("secret detail")

    for render, message in ((broken, "A picture could not be loaded from Drive."), (crashed, "The PDF could not be made.")):
        exports = Exports(cfg.exports_dir, lambda k, i, p, r=render: pool.submit(r, k, i, p))
        port = serve(s.make_server(cfg, db, port=0, exports=exports))
        sid, _ = any_set(db, "cv")
        job = json.loads(call(port, "POST", f"/api/sets/cv/{sid}/pdf", {})[1])["job"]
        time.sleep(0.2)
        assert json.loads(call(port, "GET", f"/api/exports/{job}")[1]) == {"status": "error", "message": message}


def test_unknown_set_job_and_no_exporter(cfg, db, pool):
    exports = Exports(cfg.exports_dir, lambda k, i, p: pool.submit(lambda: 1))
    port = serve(s.make_server(cfg, db, port=0, exports=exports))
    assert call(port, "POST", "/api/sets/portfolio/nope/pdf", {})[0] == 404
    assert call(port, "GET", "/api/exports/nope")[0] == 404
    bare = serve(s.make_server(cfg, db, port=0))
    sid, _ = any_set(db, "portfolio")
    assert call(bare, "POST", f"/api/sets/portfolio/{sid}/pdf", {})[0] == 503


def test_finished_exports_are_deleted_after_the_ttl(tmp_path):
    now = [1000.0]
    done = Future()
    exports = Exports(tmp_path / "exports", lambda k, i, p: done, ttl=60, clock=lambda: now[0])
    job = exports.start("cv", "x", "CV")
    job.path.write_bytes(b"%PDF")
    done.set_result(1)
    now[0] += 30
    assert exports.get(job.id) is job and job.path.exists()
    now[0] += 31
    assert exports.get(job.id) is None and not job.path.exists()


# --- print server -------------------------------------------------------------

@pytest.fixture
def print_server(cfg, db):
    ctx = s.build_context(cfg, db)
    httpd = s.ThreadingHTTPServer(("127.0.0.1", 0), pdf_export._handler(ctx, "tok", ROOT))
    port = serve(httpd)
    yield port
    httpd.shutdown()
    httpd.server_close()


def test_print_server_answers_only_its_token_and_only_get(print_server, db):
    ok = {pdf_export.TOKEN_HEADER: "tok"}
    assert call(print_server, "GET", "/portfolio.html")[0] == 403
    assert call(print_server, "GET", "/portfolio.html", headers={pdf_export.TOKEN_HEADER: "nope"})[0] == 403
    status, body, headers = call(print_server, "GET", "/portfolio.html", headers=ok)
    assert status == 200 and b"arta-print.js" in body
    assert "script-src 'self'" in headers["Content-Security-Policy"]
    assert call(print_server, "GET", "/dis-system/tokens/output.css", headers=ok)[0] == 200
    assert call(print_server, "GET", "/../CLAUDE.md", headers=ok)[0] == 404
    assert call(print_server, "GET", "/dis-system/.git/config", headers=ok)[0] == 404
    assert call(print_server, "GET", "/arta-api.js", headers=ok)[2]["Content-Type"].startswith("text/javascript")
    sid, _ = any_set(db, "portfolio")
    assert call(print_server, "GET", f"/api/sets/portfolio/{sid}", headers=ok)[0] == 200
    assert call(print_server, "POST", "/api/intake/run", {}, headers=ok)[0] == 405


def test_print_server_scales_originals_for_print(print_server):
    source = LocalProjects(FIXTURES / "projects")
    project, image = next((p, i) for p in source.list_projects() for i in p.images if i.kind == "jpeg")
    status, data, headers = call(print_server, "GET", f"/api/print/images/{project.id}/{image.id}",
                                 headers={pdf_export.TOKEN_HEADER: "tok"})
    assert status == 200 and headers["Content-Type"] == "image/jpeg" and data[:3] == b"\xff\xd8\xff"
    from PIL import Image
    import io
    small = call(print_server, "GET", f"/api/print/images/{project.id}/{image.id}?px=200",
                 headers={pdf_export.TOKEN_HEADER: "tok"})[1]
    with Image.open(io.BytesIO(small)) as im:
        assert max(im.size) <= 200          # the page asks for what its place needs
    assert call(print_server, "GET", f"/api/print/images/{project.id}/nope",
                headers={pdf_export.TOKEN_HEADER: "tok"})[0] == 404


# --- rendering in Chromium (skipped where Playwright is not installed) ----------

def pages_of(pdf: bytes) -> int:
    return len(re.findall(rb"/Type\s*/Page(?![s\w])", pdf))


@pytest.mark.parametrize("kind, name, sheets", [("portfolio", "Harbour overview", 5), ("cv", "My usual CV set", 2)])
def test_render_prints_every_sheet_of_the_set_one_per_page(cfg, db, tmp_path, kind, name, sheets, monkeypatch):
    pytest.importorskip("playwright.sync_api")
    from sources import thumb_cache
    scale = thumb_cache.print_image
    # Originals as slow as from Drive, so the progress can be watched arriving.
    monkeypatch.setattr(thumb_cache, "print_image", lambda data, px: (time.sleep(0.5), scale(data, px))[1])
    ctx = s.build_context(cfg, db)
    out = tmp_path / "out.pdf"
    seen = []
    pages = pdf_export.render(ctx, kind, set_id(db, kind, name), out, progress=seen.append)
    data = out.read_bytes()
    assert data[:5] == b"%PDF-" and pages == sheets and pages_of(data) == sheets
    # Progress: drawing, then (with pictures) each original counted in, then printing.
    assert seen[0] == {"phase": "drawing"} and seen[-1] == {"phase": "printing"}
    counts = [p for p in seen if p["phase"] == "pictures"]
    if kind == "portfolio":
        assert len(counts) >= 2 and all(p["total"] == 8 for p in counts)     # 8 pictures in this set
        done = [p["done"] for p in counts]
        assert done == sorted(done) and done[0] < done[-1] <= 8
    # Every page is the size of the artist's own PDF, 842 x 599 pt.
    boxes = {tuple(round(float(v)) for v in m) for m in re.findall(rb"/MediaBox\s*\[\s*0 0 ([\d.]+) ([\d.]+)\s*\]", data)}
    assert boxes == {(842, 599)}


def test_a_full_bleed_sheet_fills_its_page_to_the_edges(cfg, db, tmp_path):
    pytest.importorskip("playwright.sync_api")
    if not shutil.which("pdftoppm"):
        pytest.skip("pdftoppm is not installed")
    from PIL import Image
    ctx = s.build_context(cfg, db)
    out = tmp_path / "out.pdf"
    pdf_export.render(ctx, "portfolio", set_id(db, "portfolio", "Harbour overview"), out)
    # Page 2 of this set is an exhibition view, full bleed: no paper may show at its edges.
    subprocess.run(["pdftoppm", "-f", "2", "-l", "2", "-r", "36", "-png", str(out), str(tmp_path / "p")], check=True)
    with Image.open(next(tmp_path.glob("p*.png"))) as im:
        im = im.convert("RGB")
        w, h = im.size
        corners = [im.getpixel((x, y)) for x in (1, w - 2) for y in (1, h - 2)]
    assert all(sum(c) < 3 * 240 for c in corners), corners


def test_render_stops_when_asked(cfg, db, tmp_path):
    pytest.importorskip("playwright.sync_api")
    ctx = s.build_context(cfg, db)
    asked = []
    with pytest.raises(pdf_export.ExportStopped):
        pdf_export.render(ctx, "portfolio", set_id(db, "portfolio", "Harbour overview"), tmp_path / "out.pdf",
                          stopped=lambda: asked.append(1) or len(asked) > 1)
    assert not (tmp_path / "out.pdf").exists()


def test_render_of_an_empty_set_says_so(cfg, db, tmp_path):
    pytest.importorskip("playwright.sync_api")
    ctx = s.build_context(cfg, db)
    with pytest.raises(pdf_export.ExportError, match="no pages"):
        pdf_export.render(ctx, "cv", set_id(db, "cv", "Full CV for another"), tmp_path / "out.pdf")


def test_render_of_a_missing_set_says_so(cfg, db, tmp_path):
    pytest.importorskip("playwright.sync_api")
    ctx = s.build_context(cfg, db)
    with pytest.raises(pdf_export.ExportError, match="not found"):
        pdf_export.render(ctx, "portfolio", "nope", tmp_path / "out.pdf")
