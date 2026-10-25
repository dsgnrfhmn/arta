"""For You actions that need no model: archive, unarchive, promote, saved,
notes, description. Model layer and HTTP layer, against the real seed."""

import json
from pathlib import Path

import pytest

from core import sanitize
from core.config import Config
from core.db import Database
from core.seed import seed_opportunities
from models import opportunity
from tests.test_server import JSON, call, serve, url

SEED = json.loads((Path(__file__).parent / "fixtures" / "opportunities.json").read_text("utf-8"))


def ids(pred):
    return [r["id"] for r in SEED if pred(r)]


REVIEW = ids(lambda r: r["stage"] == "for_review")
ANALYSED = ids(lambda r: r["stage"] == "analysed" and not r.get("archived"))
ARCHIVED = ids(lambda r: r.get("archived"))
FILTERED = ids(lambda r: r["stage"] == "filtered")
APPLIED = ids(lambda r: r.get("application"))
EMPTY_DESC = ids(lambda r: r["stage"] == "for_review" and r["description"] == "")


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "arta.sqlite3")
    d.migrate()
    seed_opportunities(d, Path(__file__).parent / "fixtures")
    return d


@pytest.fixture
def srv(tmp_path, db):
    cfg = Config(updates="fixtures", projects="fixtures", cv="fixtures", llm="mock", port=0, data_dir=tmp_path)
    server = serve(cfg, db)
    yield server
    server.shutdown()
    server.server_close()


def post(srv, path, body):
    return call(url(srv, path), "POST", json.dumps(body).encode(), JSON)


def patch(srv, oid, body):
    return call(url(srv, "/api/opportunities/" + oid), "PATCH", json.dumps(body).encode(), JSON)


# --- listing ---------------------------------------------------------------

def test_the_four_tables_are_queries_over_one_record_set(srv):
    def got(q):
        return [i["id"] for i in call(url(srv, "/api/opportunities?" + q))[1]["items"]]

    assert sorted(got("stage=for_review&archived=false")) == sorted(REVIEW)
    assert sorted(got("stage=analysed&archived=false")) == sorted(ANALYSED)
    assert sorted(got("archived=true")) == sorted(ARCHIVED)
    assert sorted(got("stage=filtered")) == sorted(FILTERED)
    assert sorted(got("applied=true")) == sorted(APPLIED)


def test_archived_is_newest_archived_first(srv):
    items = call(url(srv, "/api/opportunities?archived=true"))[1]["items"]
    stamps = [i["archived_at"] for i in items]
    assert stamps == sorted(stamps, reverse=True)


def test_bad_flag_value_is_rejected(srv):
    assert call(url(srv, "/api/opportunities?archived=yes"))[0] == 400


# --- archive / unarchive ---------------------------------------------------

def test_archive_inserts_at_top_and_unarchive_returns_to_its_table(srv):
    oid = REVIEW[0]
    status, body, _ = post(srv, "/api/opportunities/archive", {"ids": [oid]})
    assert status == 200 and body["items"][0]["flags"]["archived"] is True
    top = call(url(srv, "/api/opportunities?archived=true"))[1]["items"][0]
    assert top["id"] == oid                                   # newest archived first
    assert top["stage"] == "for_review"                       # source table is kept
    post(srv, "/api/opportunities/unarchive", {"ids": [oid]})
    back = call(url(srv, "/api/opportunities?stage=for_review&archived=false"))[1]["items"]
    assert oid in [i["id"] for i in back]


def test_archived_opportunity_keeps_its_status(srv):
    oid = APPLIED[0]
    _, body, _ = post(srv, "/api/opportunities/archive", {"ids": [oid]})
    assert body["items"][0]["application"]["status"] == "applied"


def test_bulk_archive_is_all_or_nothing(srv):
    # second id is filtered: the whole call fails and the first stays untouched.
    status, body, _ = post(srv, "/api/opportunities/archive", {"ids": [REVIEW[0], FILTERED[0]]})
    assert status == 409 and body["error"]["code"] == "conflict"
    assert call(url(srv, "/api/opportunities/" + REVIEW[0]))[1]["flags"]["archived"] is False


def test_archive_twice_and_unarchive_of_active_are_conflicts(srv):
    assert post(srv, "/api/opportunities/archive", {"ids": [ARCHIVED[0]]})[0] == 409
    assert post(srv, "/api/opportunities/unarchive", {"ids": [REVIEW[0]]})[0] == 409


def test_unknown_id_is_404_and_bad_ids_are_400(srv):
    assert post(srv, "/api/opportunities/archive", {"ids": ["nope"]})[0] == 404
    for bad in ({}, {"ids": []}, {"ids": "x"}, {"ids": [1]}, {"ids": [REVIEW[0], REVIEW[0]]}):
        assert post(srv, "/api/opportunities/archive", bad)[0] == 400, bad
    assert call(url(srv, "/api/opportunities/archive"), "POST", b"[]", JSON)[0] == 400


# --- promote ---------------------------------------------------------------

def test_promote_moves_filtered_to_review_and_clears_reason(srv):
    oid = FILTERED[0]
    _, body, _ = post(srv, "/api/opportunities/promote", {"ids": [oid]})
    item = body["items"][0]
    assert item["stage"] == "for_review" and item["fields"]["filter_reason"] is None
    assert post(srv, "/api/opportunities/promote", {"ids": [oid]})[0] == 409


# --- saved / notes / description ------------------------------------------

def test_saved_single_and_bulk(srv):
    oid = next(i for i in REVIEW if not next(r for r in SEED if r["id"] == i).get("saved"))
    assert patch(srv, oid, {"saved": True})[1]["flags"]["saved"] is True
    _, body, _ = post(srv, "/api/opportunities/saved", {"ids": REVIEW[:3], "saved": False})
    assert [i["flags"]["saved"] for i in body["items"]] == [False, False, False]
    assert post(srv, "/api/opportunities/saved", {"ids": REVIEW[:1], "saved": "yes"})[0] == 400


def test_notes_are_stored_once_and_survive_a_fresh_read(srv, db):
    oid = ANALYSED[0]
    assert patch(srv, oid, {"notes": "call on Monday"})[1]["fields"]["notes"] == "call on Monday"
    assert opportunity.get(db, oid).notes == "call on Monday"
    assert patch(srv, oid, {"notes": "x" * 2001})[0] == 400


def test_description_add_edit_clear_in_for_review_only(srv):
    oid = EMPTY_DESC[0]
    assert patch(srv, oid, {"description": "  Open call for painters.  "})[1]["fields"]["description"] == "Open call for painters."
    assert patch(srv, oid, {"description": ""})[1]["fields"]["description"] == ""
    assert patch(srv, ANALYSED[0], {"description": "x"})[0] == 409
    assert patch(srv, FILTERED[0], {"description": "x"})[0] == 409


def test_description_is_sanitized_on_save(srv):
    _, body, _ = patch(srv, EMPTY_DESC[0], {"description": "<b>Fee</b> waived<script>alert(1)</script>\x00"})
    assert body["fields"]["description"] == "Fee waived"


def test_patch_rejects_fields_that_are_not_editable(srv):
    for bad in ({"name": "x"}, {"stage": "analysed"}, {"archived": True}, {}, []):
        assert patch(srv, REVIEW[0], bad)[0] == 400, bad
    assert patch(srv, "nope", {"notes": "x"})[0] == 404


def test_writes_need_json(srv):
    assert call(url(srv, "/api/opportunities/archive"), "POST", b'{"ids":["x"]}', {})[0] == 415


# --- sanitize --------------------------------------------------------------

@pytest.mark.parametrize("raw,want", [
    ("plain", "plain"),
    ("a &amp; b", "a & b"),
    ("<p>one</p><style>x{}</style>two", "onetwo"),
    ("line1\r\nline2", "line1\nline2"),
    ("tab\there", "tab\there"),
    ("bell\x07!", "bell!"),
    ("  padded  ", "padded"),
])
def test_plain_text(raw, want):
    assert sanitize.plain_text(raw, 100) == want


def test_plain_text_rejects_non_text_and_overlong():
    with pytest.raises(ValueError):
        sanitize.plain_text(5, 10)
    with pytest.raises(ValueError):
        sanitize.plain_text("x" * 11, 10)
