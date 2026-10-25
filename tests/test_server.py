import json
import os
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from core.config import Config
from core.db import Database
from core.seed import seed_opportunities
from web import server as s

SEED = json.loads((Path(__file__).parent / "fixtures" / "opportunities.json").read_text("utf-8"))
GRANT = next(r for r in SEED if r["type"] == "grant")

REPO = Path(__file__).resolve().parent.parent
JSON = {"Content-Type": "application/json"}


def serve(cfg, db, router=None):
    srv = s.make_server(cfg, db, port=0, router=router)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def url(srv, path):
    return f"http://127.0.0.1:{srv.server_address[1]}{path}"


def call(u, method="GET", body=None, headers=None):
    req = urllib.request.Request(u, method=method, data=body, headers=headers or {})
    if not req.has_header("Origin"):   # a browser sends it on every write
        req.add_header("Origin", "http://" + req.host)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read()), r.headers
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read()), e.headers


@pytest.fixture
def cfg(tmp_path):
    return Config(updates="fixtures", projects="fixtures", cv="fixtures", llm="mock", port=0, data_dir=tmp_path)


@pytest.fixture
def db(cfg):
    d = Database(cfg.db_path)
    d.migrate()
    seed_opportunities(d, cfg.fixtures_dir)
    return d


@pytest.fixture
def srv(cfg, db):
    server = serve(cfg, db)
    yield server
    server.shutdown()
    server.server_close()


def test_binds_loopback_only(srv):
    assert srv.server_address[0] == "127.0.0.1"


def test_health_reports_mock_llm_and_source(srv):
    status, body, _ = call(url(srv, "/api/health"))
    assert status == 200
    assert body["llm"] == "mock" and body["schema_version"] >= 1
    assert body["sources"] == {"intake": [], "updates": "fixtures", "projects": "fixtures", "cv": "fixtures"}


def test_list_returns_seeded_data_and_is_not_cacheable(srv):
    status, body, headers = call(url(srv, "/api/opportunities"))
    assert status == 200 and len(body["items"]) == len(SEED)
    assert headers["Cache-Control"] == "no-store"


def test_stage_filter(srv):
    _, body, _ = call(url(srv, "/api/opportunities?stage=analysed"))
    assert {i["id"] for i in body["items"]} == {r["id"] for r in SEED if r["stage"] == "analysed"}
    assert call(url(srv, "/api/opportunities?stage=bogus"))[0] == 400


def test_get_one_and_404(srv):
    assert call(url(srv, "/api/opportunities/" + GRANT["id"]))[1]["fields"]["type"] == "grant"
    status, body, _ = call(url(srv, "/api/opportunities/nope"))
    assert status == 404 and body["error"]["code"] == "not_found"


def test_unknown_route_404_and_wrong_method_405(srv):
    assert call(url(srv, "/api/nothing"))[0] == 404
    assert call(url(srv, "/api/health"), "POST", b"{}", JSON)[0] == 405


@pytest.fixture
def echo(cfg, db):
    r = s.Router()
    r.add("POST", "/api/echo", lambda req: (200, req.body))
    r.add("GET", "/api/boom", lambda req: 1 / 0)
    server = serve(cfg, db, r)
    yield server
    server.shutdown()
    server.server_close()


@pytest.mark.parametrize("headers", [{"Content-Type": "text/plain"},
                                     {"Content-Type": "application/x-www-form-urlencoded"}, {}])
def test_write_requires_json_content_type(echo, headers):
    assert call(url(echo, "/api/echo"), "POST", b'{"a":1}', headers)[0] == 415


def test_write_body_handling(echo):
    u = url(echo, "/api/echo")
    assert call(u, "POST", b'{"a":1}', JSON)[1] == {"a": 1}
    assert call(u, "POST", b"{oops", JSON)[0] == 400
    assert call(u, "POST", b'"' + b"x" * 1_000_001 + b'"', JSON)[0] == 413


def test_unhandled_error_is_generic_500(echo):
    status, body, _ = call(url(echo, "/api/boom"))
    assert status == 500
    assert body == {"error": {"code": "internal", "message": "Internal error."}}


def start(tmp_path, config_text):
    """Run `python -m web.server` against a config file; it must refuse to start."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("ARTA_")}
    env["ARTA_CONFIG"] = str(tmp_path / "config.toml")
    if config_text is not None:
        (tmp_path / "config.toml").write_text(config_text)
    return subprocess.run([sys.executable, "-m", "web.server"], cwd=REPO, env=env,
                          capture_output=True, text=True, timeout=20)


def test_start_without_a_config_file_says_to_copy_the_example(tmp_path):
    p = start(tmp_path, None)
    assert p.returncode == 2
    assert "No configuration at" in p.stderr and "config.example.toml" in p.stderr


def test_start_without_a_source_fails_with_clear_message(tmp_path):
    text = (REPO / "config.example.toml").read_text()
    text = text.replace('source = "fixtures"', 'source = ""', 1).replace(     # the first: [updates]
        'data_dir = "/Users/you/Library/Application Support/arta"', f'data_dir = "{tmp_path / "data"}"')
    p = start(tmp_path, text)
    assert p.returncode == 2
    assert "updates.source='' is invalid" in p.stderr
    assert not (tmp_path / "data").exists()  # failed before touching state


# --- Host, Origin and the pages ---------------------------------------------------------------

def raw(srv, method, path, headers, body=None):
    """One request with exactly these headers (http.client adds no Host or Origin of its own)."""
    import http.client
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
    conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
    for k, v in headers.items():
        conn.putheader(k, v)
    if body is not None:
        conn.putheader("Content-Length", str(len(body)))
    conn.endheaders(body)
    r = conn.getresponse()
    data = r.read()
    conn.close()
    return r.status, data, r


def own(srv):
    return f"127.0.0.1:{srv.server_address[1]}"


@pytest.mark.parametrize("host", ["", "evil.example", "evil.example:{port}", "127.0.0.1:1", "localhost"])
def test_a_foreign_host_is_refused_before_any_route_or_file(echo, host):
    headers = {"Host": host.format(port=echo.server_address[1])} if host else {}
    for path in ("/api/health", "/for-you.html", "/api/nothing"):
        status, body, _ = raw(echo, "GET", path, headers)
        assert status == 403 and json.loads(body)["error"]["code"] == "bad_host"


@pytest.mark.parametrize("host", ["127.0.0.1:{port}", "localhost:{port}", "LOCALHOST:{port}"])
def test_own_host_names_are_accepted(srv, host):
    assert raw(srv, "GET", "/api/health", {"Host": host.format(port=srv.server_address[1])})[0] == 200


@pytest.mark.parametrize("origin", [None, "", "null", "http://evil.example", "http://127.0.0.1:1",
                                    "https://127.0.0.1:{port}"])
@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_a_write_without_this_servers_origin_is_refused(echo, origin, method):
    headers = {"Host": own(echo), **JSON}
    if origin is not None:
        headers["Origin"] = origin.format(port=echo.server_address[1])
    status, body, _ = raw(echo, method, "/api/echo", headers, b'{"a":1}')
    assert status == 403 and json.loads(body)["error"]["code"] == "bad_origin"


@pytest.mark.parametrize("origin", ["http://127.0.0.1:{port}", "http://localhost:{port}"])
def test_a_write_from_this_server_passes(echo, origin):
    headers = {"Host": own(echo), "Origin": origin.format(port=echo.server_address[1]), **JSON}
    status, body, _ = raw(echo, "POST", "/api/echo", headers, b'{"a":1}')
    assert status == 200 and json.loads(body) == {"a": 1}


def test_no_cors_headers_and_security_headers_on_every_answer(echo):
    for path in ("/api/health", "/api/nothing", "/for-you.html", "/missing.html"):
        _, _, r = raw(echo, "GET", path, {"Host": own(echo), "Origin": "http://evil.example"})
        assert not [h for h in r.headers if h.lower().startswith("access-control-")]
        assert "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]
        assert r.headers["X-Content-Type-Options"] == "nosniff"


def test_pages_are_served_and_index_is_for_you(srv):
    status, body, r = raw(srv, "GET", "/", {"Host": own(srv)})
    assert status == 200 and body == (REPO / "www" / "for-you.html").read_bytes()
    assert r.headers["Content-Type"] == "text/html; charset=utf-8"
    status, _, r = raw(srv, "GET", "/arta-api.js", {"Host": own(srv)})
    assert status == 200 and r.headers["Content-Type"].startswith("text/javascript")


@pytest.mark.parametrize("path", ["/../core/config.py", "/%2e%2e/core/config.py", "/.hidden",
                                  "/dis-system/../core/config.py", "/dis-system/.git/config", "/nope.html"])
def test_nothing_outside_the_page_roots_is_served(srv, path):
    assert raw(srv, "GET", path, {"Host": own(srv)})[0] == 404


def test_pages_take_get_only(srv):
    headers = {"Host": own(srv), "Origin": "http://" + own(srv), **JSON}
    assert raw(srv, "POST", "/for-you.html", headers, b"{}")[0] == 405
