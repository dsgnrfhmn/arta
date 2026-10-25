"""web/jobs.py: heavy handlers run in a worker process, the rest of the API in its own."""

import json
import os
import threading
import time
import urllib.request

import pytest

from core.config import Config
from core.db import Database
from web import server as s
from web.jobs import Jobs


@pytest.fixture
def stack(tmp_path):
    cfg = Config(updates="fixtures", projects="fixtures", cv="fixtures", llm="mock", port=0, data_dir=tmp_path / "data", intake=("fixtures",))
    db = Database(cfg.db_path)
    db.migrate()
    jobs = Jobs(cfg)
    server = s.make_server(cfg, db, port=0, jobs=jobs)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server, jobs
    server.shutdown()
    server.server_close()
    jobs.close()


def call(server, method, path, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{server.server_address[1]}{path}", method=method, data=data,
                                 headers={"Content-Type": "application/json"} if data else {})
    if not req.has_header("Origin"):   # a browser sends it on every write
        req.add_header("Origin", "http://" + req.host)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def worker_pid(jobs):
    return jobs._pool.submit(os.getpid).result()


def test_heavy_handlers_are_the_mail_page_and_claude_ones():
    assert {h.__name__ for h in s.HEAVY} == {
        "intake_run", "tracker_refresh", "analyse_opportunities", "prepare_opportunities",
        "ai_edit_portfolio_set", "ai_edit_cv_set"}


def test_intake_runs_in_the_worker_and_the_api_sees_what_it_stored(stack):
    server, jobs = stack
    assert worker_pid(jobs) != os.getpid()
    status, body = call(server, "POST", "/api/intake/run", {})
    assert status == 200 and body["added"] + body["filtered"] == 4
    assert len(call(server, "GET", "/api/opportunities")[1]["items"]) == 4      # one database, two processes
    assert call(server, "POST", "/api/intake/run", {})[1]["added"] == 0


def test_an_error_in_the_worker_is_the_same_http_error(stack):
    server, _ = stack
    status, body = call(server, "POST", "/api/sets/portfolio/nope/ai-edit", {"prompt": "Shorter texts"})
    assert status == 404 and body["error"]["code"] == "not_found"


def test_the_api_answers_while_the_worker_is_busy(stack):
    server, jobs = stack
    worker_pid(jobs)                       # started
    busy = jobs._pool.submit(time.sleep, 3)
    started = time.monotonic()
    assert call(server, "GET", "/api/health")[0] == 200
    assert time.monotonic() - started < 1
    busy.result()


def test_a_dead_worker_fails_one_request_and_is_replaced(stack):
    server, jobs = stack
    os.kill(worker_pid(jobs), 9)
    time.sleep(0.5)
    status, body = call(server, "POST", "/api/intake/run", {})
    assert status == 500 and "worker" in body["error"]["message"]
    assert call(server, "POST", "/api/intake/run", {})[0] == 200
