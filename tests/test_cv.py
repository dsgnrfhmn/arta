"""CV: master blocks and referents, CV sets over the shared store, endpoints."""

import json
import threading
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import pytest

from core.config import Config
from core.db import Database
from core.errors import Conflict, NotFound
from core.seed import seed_cv_sets, seed_opportunities, seed_portfolio_sets
from sets import cv_sets, store
from sources.local_cv import LocalCv
from sources.local_projects import LocalProjects
from sources.project_source import NotFound as SourceNotFound
from sources.project_source import SourceError
from web import server as s

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"
T = "2026-09-20T12:00:00Z"
JSON_H = {"Content-Type": "application/json"}


@pytest.fixture
def cv():
    return LocalCv(FIXTURES / "cv" / "master.json")


@pytest.fixture
def db(tmp_path, cv):
    d = Database(tmp_path / "arta.sqlite3")
    d.migrate()
    seed_opportunities(d, FIXTURES)
    seed_portfolio_sets(d, FIXTURES, LocalProjects(ROOT / "tests" / "fixtures" / "projects"))
    seed_cv_sets(d, FIXTURES, cv)
    return d


def sid(db, name):
    with db.read() as c:
        return next(r.id for r in store.list_sets(c, "cv") if r.name == name)


def get(db, set_id):
    with db.read() as c:
        return store.get(c, "cv", set_id)


def texts(record):
    return [b["text"] for b in record.content]


def full(cv):
    return {b.id: b for b in cv.get_blocks("full")}


def new_set(db):
    with db.tx() as c:
        return store.create(c, "cv", T).id


def add(db, cv, set_id, referent, block_id):
    with db.tx() as c:
        return cv_sets.add_block(c, cv, set_id, {"referent_id": referent, "block_id": block_id})


# --- the source ---------------------------------------------------------------

def test_referents_and_uuid_ids(cv):
    assert [(r.id, r.name) for r in cv.list_referents()] == [("short", "Short CV"), ("middle", "Middle CV"), ("full", "Full CV")]
    blocks = cv.get_blocks("full")
    assert len(blocks) == 13
    for b in blocks:
        assert str(uuid.UUID(b.id)) == b.id and b.kind in ("heading", "text")
    # Short and Middle are subsets of Full, in the same order, with the same ids.
    order = [b.id for b in blocks]
    for key in ("short", "middle"):
        ids = [b.id for b in cv.get_blocks(key)]
        assert ids == [i for i in order if i in ids] and len(ids) < len(order)


def test_unknown_referent_is_not_found(cv):
    with pytest.raises(SourceNotFound):
        cv.get_blocks("nope")


def test_ids_do_not_depend_on_text_or_position(cv):
    """Two blocks with the same text (Figma shows one twice) still have two ids."""
    blocks = cv.get_blocks("full")
    assert len({b.text for b in blocks}) < len(blocks)
    assert len({b.id for b in blocks}) == len(blocks)


@pytest.mark.parametrize("mutate,message", [
    (lambda d: d["blocks"].append(dict(d["blocks"][0])), "twice"),
    (lambda d: d["blocks"][0].update(id="not-a-uuid"), "UUID"),
    (lambda d: d["blocks"][0].update(kind="banner"), "kind"),
    (lambda d: d["referents"][0]["blocks"].append(str(uuid.uuid4())), "unknown block"),
    (lambda d: d["referents"][0]["blocks"].append(d["referents"][0]["blocks"][0]), "twice"),
])
def test_a_fixture_that_does_not_hold_together_raises(tmp_path, mutate, message):
    data = json.loads((FIXTURES / "cv" / "master.json").read_text("utf-8"))
    mutate(data)
    path = tmp_path / "master.json"
    path.write_text(json.dumps(data), "utf-8")
    with pytest.raises(SourceError, match=message):
        LocalCv(path)


# --- seeding --------------------------------------------------------------------

def test_seed_makes_three_cv_sets_and_only_once(db, cv):
    with db.read() as c:
        names = [r.name for r in store.list_sets(c, "cv")]
    assert sorted(names) == ["Full CV for another", "My usual CV set", "Short CV for Coastal Residency"]
    assert seed_cv_sets(db, FIXTURES, cv) == 0


def test_cv_seed_is_not_blocked_by_existing_portfolio_sets(tmp_path, cv):
    """A database from before A3 already has portfolio sets and no CV set."""
    d = Database(tmp_path / "old.sqlite3")
    d.migrate()
    seed_opportunities(d, FIXTURES)
    assert seed_portfolio_sets(d, FIXTURES, LocalProjects(ROOT / "tests" / "fixtures" / "projects")) == 6
    assert seed_cv_sets(d, FIXTURES, cv) == 3
    assert seed_portfolio_sets(d, FIXTURES, LocalProjects(ROOT / "tests" / "fixtures" / "projects")) == 0


def test_seed_raises_on_a_block_that_is_not_in_the_master(tmp_path, cv):
    fx = tmp_path / "fx"
    (fx / "cv").mkdir(parents=True)
    (fx / "cv" / "sets.json").write_text(json.dumps([{"name": "x", "created_at": T, "blocks": [str(uuid.uuid4())]}]))
    d = Database(tmp_path / "a.sqlite3")
    d.migrate()
    with pytest.raises(ValueError, match="unknown block"):
        seed_cv_sets(d, fx, cv)


def test_the_saved_seed_set_resets_to_itself(db):
    usual = get(db, sid(db, "My usual CV set"))
    assert usual.saved and len(usual.content) == 9
    with db.tx() as c:
        cv_sets.delete_block(c, usual.id, usual.content[0]["id"])
        assert len(store.reset(c, "cv", usual.id).content) == 9


# --- referents --------------------------------------------------------------------

def test_referents_are_master_cvs_then_saved_sets(db, cv):
    with db.read() as c:
        items = cv_sets.list_referents(c, cv)
    assert [(i["id"], i["kind"]) for i in items[:3]] == [("short", "master"), ("middle", "master"), ("full", "master")]
    assert [i["name"] for i in items[3:]] == ["My usual CV set"]       # only saved sets
    assert items[3]["id"] == "set:" + sid(db, "My usual CV set") and items[3]["kind"] == "set"


def test_an_archived_saved_set_is_not_a_referent(db, cv):
    usual = sid(db, "My usual CV set")
    with db.tx() as c:
        store.archive(c, "cv", usual, T)
    with db.read() as c:
        assert [i["kind"] for i in cv_sets.list_referents(c, cv)] == ["master"] * 3
        with pytest.raises(NotFound):
            cv_sets.referent_blocks(c, cv, "set:" + usual)


def test_a_set_referent_serves_the_sets_own_blocks(db, cv):
    usual = get(db, sid(db, "My usual CV set"))
    with db.read() as c:
        assert cv_sets.referent_blocks(c, cv, "set:" + usual.id) == usual.content
        with pytest.raises(NotFound):
            cv_sets.referent_blocks(c, cv, "set:missing")
        with pytest.raises(SourceNotFound):
            cv_sets.referent_blocks(c, cv, "nope")


# --- editing ----------------------------------------------------------------------

def test_add_appends_with_the_referents_id_and_text(db, cv):
    target = new_set(db)
    first, second = cv.get_blocks("short")[:2]
    add(db, cv, target, "short", first.id)
    rec = add(db, cv, target, "short", second.id)
    assert rec.content == [first.to_api(), second.to_api()]
    assert rec.can_undo


def test_a_block_cannot_be_added_twice(db, cv):
    target = new_set(db)
    b = cv.get_blocks("full")[0]
    add(db, cv, target, "full", b.id)
    with pytest.raises(Conflict):
        add(db, cv, target, "short", b.id)      # same block from another referent
    assert len(get(db, target).content) == 1


def test_two_blocks_with_the_same_text_are_both_addable(db, cv):
    target = new_set(db)
    twins = [b for b in cv.get_blocks("full") if b.text.startswith("Gallery educator, “Looking slowly”")]
    assert len(twins) == 2
    for b in twins:
        add(db, cv, target, "full", b.id)
    content = get(db, target).content
    assert [b["kind"] for b in content] == ["heading", "text", "text"]      # both entries, under one heading
    assert len({b["id"] for b in content}) == 3


@pytest.mark.parametrize("body,exc", [
    ({"referent_id": "full", "block_id": str(uuid.uuid4())}, NotFound),        # not in that referent
    ({"referent_id": "nope", "block_id": str(uuid.uuid4())}, SourceNotFound),
    ({"referent_id": "full", "block_id": "12345"}, ValueError),
    ({"referent_id": "full"}, ValueError),
    ({"block_id": str(uuid.uuid4())}, ValueError),
    ({"referent_id": "full", "block_id": str(uuid.uuid4()), "text": "x"}, ValueError),
    ([], ValueError),
])
def test_add_rejects_bad_input_and_records_no_step(db, cv, body, exc):
    target = new_set(db)
    with pytest.raises(exc):
        with db.tx() as c:
            cv_sets.add_block(c, cv, target, body)
    assert not get(db, target).content and not get(db, target).can_undo


def test_add_from_a_saved_set_copies_the_sets_edited_text(db, cv):
    usual = get(db, sid(db, "My usual CV set"))
    block = usual.content[1]
    with db.tx() as c:
        cv_sets.edit_block(c, usual.id, block["id"], "Edited for this set.")
    target = new_set(db)
    rec = add(db, cv, target, "set:" + usual.id, block["id"])
    assert rec.content[-1] == {**block, "text": "Edited for this set."}
    assert [b["kind"] for b in rec.content] == ["heading", "text"]          # its section heading comes with it


def test_edit_changes_the_set_only_never_the_referent(db, cv):
    usual = get(db, sid(db, "My usual CV set"))
    block = usual.content[1]
    with db.tx() as c:
        rec = cv_sets.edit_block(c, usual.id, block["id"], "  New text.  ")
    assert rec.content[1] == {**block, "text": "New text."}
    assert full(cv)[block["id"]].text == block["text"]
    other = get(db, sid(db, "Short CV for Coastal Residency"))
    assert other.content[1]["text"] == block["text"]         # another set is untouched


def test_edit_strips_markup_and_refuses_empty(db):
    usual = get(db, sid(db, "My usual CV set"))
    block_id = usual.content[0]["id"]
    with db.tx() as c:
        rec = cv_sets.edit_block(c, usual.id, block_id, "<b>Bio</b><script>alert(1)</script> now")
    assert rec.content[0]["text"] == "Bio now"
    for bad in ("", "   ", "<i></i>", 5):
        with pytest.raises(ValueError):
            with db.tx() as c:
                cv_sets.edit_block(c, usual.id, block_id, bad)
    with pytest.raises(ValueError):
        with db.tx() as c:
            cv_sets.edit_block(c, usual.id, block_id, "x" * (cv_sets.TEXT_MAX + 1))


def test_edit_and_delete_of_a_block_that_is_not_in_the_set_are_404(db):
    target = new_set(db)
    for fn in (lambda c: cv_sets.edit_block(c, target, str(uuid.uuid4()), "x"),
               lambda c: cv_sets.delete_block(c, target, str(uuid.uuid4()))):
        with pytest.raises(NotFound):
            with db.tx() as c:
                fn(c)


def test_edit_to_the_same_text_is_not_an_undo_step(db):
    usual = get(db, sid(db, "My usual CV set"))
    with db.tx() as c:
        rec = cv_sets.edit_block(c, usual.id, usual.content[0]["id"], usual.content[0]["text"])
    assert not rec.can_undo


def test_delete_then_readd_then_undo_each(db, cv):
    usual = get(db, sid(db, "My usual CV set"))
    before = usual.content
    gone = before[2]
    with db.tx() as c:
        rec = cv_sets.delete_block(c, usual.id, gone["id"])
    assert gone["id"] not in [b["id"] for b in rec.content]
    rec = add(db, cv, usual.id, "full", gone["id"])
    ids = [b["id"] for b in rec.content]
    assert ids.count(gone["id"]) == 1                           # re-added once, same id, in its section
    with db.tx() as c:
        assert store.undo_last(c, "cv", usual.id).content == [b for b in before if b["id"] != gone["id"]]
        assert store.undo_last(c, "cv", usual.id).content == before
        with pytest.raises(Conflict):
            store.undo_last(c, "cv", usual.id)


def test_add_all_puts_in_every_missing_block_as_add_would_in_one_undo_step(db, cv):
    usual = get(db, sid(db, "My usual CV set"))
    before = usual.content
    with db.tx() as c:
        rec = cv_sets.add_all(c, cv, usual.id, {"referent_id": "full"})
    ids = [b["id"] for b in rec.content]
    assert set(ids) == set(full(cv)) | {b["id"] for b in before}
    assert len(ids) == len(set(ids))                            # nothing twice
    # Each block where add_block puts it: adding them one by one gives the same set.
    one_by_one = new_set(db)
    for b in cv.get_blocks("full"):
        add(db, cv, one_by_one, "full", b.id)
    fresh = new_set(db)
    with db.tx() as c:
        assert cv_sets.add_all(c, cv, fresh, {"referent_id": "full"}).content == get(db, one_by_one).content
        assert store.undo_last(c, "cv", usual.id).content == before   # one step back
        again = cv_sets.add_all(c, cv, fresh, {"referent_id": "full"})
    assert again.content == get(db, one_by_one).content          # all in already: no change


def test_remove_all_empties_the_set_in_one_undo_step(db):
    usual = get(db, sid(db, "My usual CV set"))
    with db.tx() as c:
        assert cv_sets.remove_all(c, usual.id).content == []
        assert store.undo_last(c, "cv", usual.id).content == usual.content


@pytest.mark.parametrize("body", [None, [], {}, {"referent_id": ""}, {"referent_id": "full", "x": 1}])
def test_add_all_rejects_bad_input(db, cv, body):
    target = new_set(db)
    with db.tx() as c, pytest.raises(ValueError):
        cv_sets.add_all(c, cv, target, body)


def test_reorder_keeps_ids_and_is_undoable(db):
    usual = get(db, sid(db, "My usual CV set"))
    ids = [b["id"] for b in usual.content]
    slots = cv_sets._slots(usual.content)
    i, j = next((i, j) for i in range(len(ids)) for j in range(i + 1, len(ids)) if slots[i] == slots[j])
    swapped = list(ids)
    swapped[i], swapped[j] = swapped[j], swapped[i]
    with db.tx() as c:
        rec = cv_sets.reorder(c, usual.id, swapped)
    assert [b["id"] for b in rec.content] == swapped
    assert texts(rec)[i] == texts(usual)[j] and texts(rec)[j] == texts(usual)[i]
    with db.tx() as c:
        assert store.undo_last(c, "cv", usual.id).content == usual.content


def seeded(db, rows):
    """A new CV set holding [(kind, text)] in that order; returns its id and the block ids."""
    set_id = new_set(db)
    blocks = [{"id": str(uuid.UUID(int=100 + n)), "kind": kind, "text": text} for n, (kind, text) in enumerate(rows)]
    with db.tx() as c:
        store.mutate(c, "cv", set_id, lambda _: blocks)
    return set_id, [b["id"] for b in blocks]


ROWS = [("heading", "EDUCATION"), ("text", "2024\tA"), ("text", "2024\tB"), ("text", "2021\tC"),
        ("heading", "SOLO"), ("text", "2024\tD"), ("text", "no year"), ("text", "also no year")]


def reorder(db, set_id, ids):
    with db.tx() as c:
        return cv_sets.reorder(c, set_id, ids)


def test_blocks_sort_within_one_year_of_a_section(db):
    set_id, ids = seeded(db, ROWS)
    swapped = [ids[0], ids[2], ids[1], *ids[3:]]                         # A and B: both 2024, EDUCATION
    assert [b["id"] for b in reorder(db, set_id, swapped).content] == swapped
    undated = [*ids[:6], ids[7], ids[6]]                                  # the two entries with no year
    assert [b["id"] for b in reorder(db, set_id, undated).content][-2:] == [ids[7], ids[6]]


@pytest.mark.parametrize("name, build", [
    ("another year in the section", lambda i: [i[0], i[3], i[2], i[1], *i[4:]]),
    ("another section, same year", lambda i: [i[0], i[5], i[2], i[3], i[4], i[1], i[6], i[7]]),
    ("a heading", lambda i: [i[4], i[1], i[2], i[3], i[0], *i[5:]]),
    ("a dated entry among the undated", lambda i: [*i[:5], i[6], i[5], i[7]]),
])
def test_blocks_cannot_leave_their_year(db, name, build):
    set_id, ids = seeded(db, ROWS)
    with pytest.raises(ValueError, match="within one year"):
        reorder(db, set_id, build(ids))
    assert [b["id"] for b in get(db, set_id).content] == ids             # nothing moved


@pytest.mark.parametrize("bad", [None, "x", [], ["a"], "dup"])
def test_reorder_must_be_a_permutation(db, bad):
    usual = get(db, sid(db, "My usual CV set"))
    ids = [b["id"] for b in usual.content]
    if bad == "dup":
        bad = ids[:-1] + [ids[0]]
    with pytest.raises(ValueError):
        with db.tx() as c:
            cv_sets.reorder(c, usual.id, bad)


def test_undo_runs_across_edit_kinds_up_to_the_limit(db, cv):
    target = new_set(db)
    blocks = cv.get_blocks("full")
    for b in blocks:
        add(db, cv, target, "full", b.id)                     # 13 steps
    with db.tx() as c:
        for i in range(8):
            cv_sets.edit_block(c, target, blocks[0].id, f"v{i}")     # 8 more: 21 in all, 20 kept
    n = 0
    with db.tx() as c:
        while True:
            try:
                store.undo_last(c, "cv", target)
            except Conflict:
                break
            n += 1
    assert n == 20


def test_an_archived_cv_set_is_read_only(db, cv):
    usual = sid(db, "My usual CV set")
    block = get(db, usual).content[0]
    with db.tx() as c:
        store.archive(c, "cv", usual, T)
    for fn in (lambda c: cv_sets.edit_block(c, usual, block["id"], "x"),
               lambda c: cv_sets.delete_block(c, usual, block["id"]),
               lambda c: cv_sets.reorder(c, usual, [block["id"]]),
               lambda c: cv_sets.add_block(c, cv, usual, {"referent_id": "full", "block_id": full(cv).popitem()[0]})):
        with pytest.raises(Conflict):
            with db.tx() as c:
                fn(c)


def test_kinds_do_not_mix(db, cv):
    with db.read() as c:
        portfolio = next(r.id for r in store.list_sets(c, "portfolio"))
    with pytest.raises(NotFound):
        with db.read() as c:
            store.get(c, "cv", portfolio)
    with pytest.raises(NotFound):
        with db.tx() as c:
            cv_sets.delete_block(c, portfolio, str(uuid.uuid4()))


def test_describe_and_preview_are_mock_paginated(db):
    usual = get(db, sid(db, "My usual CV set"))
    d = cv_sets.describe(usual)
    assert d["kind"] == "cv" and d["entries"] == usual.content
    assert d["pages"] == 2 and d["pages_origin"] == "mock"      # 9 blocks, 6 to a page
    p = cv_sets.preview(usual)
    assert [len(x["entries"]) for x in p["pages"]] == [6, 3] and p["page_count"] == 2
    empty = get(db, sid(db, "Full CV for another"))
    assert cv_sets.describe(empty)["pages"] == 0


# --- HTTP ---------------------------------------------------------------------

@pytest.fixture
def srv(db, tmp_path, cv):
    cfg = Config(updates="fixtures", projects="fixtures", cv="fixtures", llm="mock", port=0, data_dir=tmp_path / "data")
    server = s.make_server(cfg, db, port=0, projects=LocalProjects(ROOT / "tests" / "fixtures" / "projects"), cv=cv)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()
    server.server_close()


def call(srv, method, path, body=None, headers=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{srv.server_address[1]}{path}", method=method, data=data,
                                 headers=headers if headers is not None else (JSON_H if data else {}))
    if not req.has_header("Origin"):   # a browser sends it on every write
        req.add_header("Origin", "http://" + req.host)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


BASE = "/api/sets/cv"


def test_list_open_create_over_http(srv, db):
    status, body = call(srv, "GET", BASE)
    assert status == 200 and len(body["items"]) == 3
    usual = next(i for i in body["items"] if i["name"] == "My usual CV set")
    assert usual["kind"] == "cv" and usual["saved"] and usual["entry_count"] == 9
    status, one = call(srv, "GET", f"{BASE}/{usual['id']}")
    assert status == 200 and len(one["entries"]) == 9 and one["pages_origin"] == "mock"
    assert set(one["entries"][0]) == {"id", "kind", "text"}
    status, created = call(srv, "POST", BASE, {})
    assert status == 201 and created["name"] == "CV Set 1" and created["entries"] == []
    # A portfolio set is not reachable as a CV set, nor the other way round.
    status, pl = call(srv, "GET", "/api/sets/portfolio")
    assert call(srv, "GET", f"{BASE}/{pl['items'][0]['id']}")[0] == 404
    assert call(srv, "GET", f"/api/sets/portfolio/{usual['id']}")[0] == 404
    assert call(srv, "GET", "/api/sets/nonsense")[0] == 404


def test_referents_over_http(srv):
    status, body = call(srv, "GET", "/api/cv-referents")
    assert status == 200 and [i["name"] for i in body["items"]] == ["Short CV", "Middle CV", "Full CV", "My usual CV set"]
    status, blocks = call(srv, "GET", "/api/cv-referents/full/blocks")
    assert status == 200 and len(blocks["items"]) == 13
    status, mine = call(srv, "GET", "/api/cv-referents/" + urllib.request.quote(body["items"][3]["id"], safe="") + "/blocks")
    assert status == 200 and len(mine["items"]) == 9
    assert call(srv, "GET", "/api/cv-referents/nope/blocks")[0] == 404
    # An encoded slash is one segment: it names no referent, and does not reach another route.
    assert call(srv, "GET", "/api/cv-referents/a%2Fb/blocks")[0] == 404
    assert call(srv, "GET", "/api/cv-referents/..%2F..%2Fhealth/blocks")[0] == 404
    assert call(srv, "GET", "/api/cv-referents/set:missing/blocks")[0] == 404


def test_add_all_and_remove_all_over_http(srv):
    _, created = call(srv, "POST", BASE, {"name": "All of it"})
    sid_ = created["id"]
    _, blocks = call(srv, "GET", "/api/cv-referents/full/blocks")
    status, body = call(srv, "POST", f"{BASE}/{sid_}/blocks/all", {"referent_id": "full"})
    assert status == 200 and {e["id"] for e in body["entries"]} == {b["id"] for b in blocks["items"]}
    assert call(srv, "POST", f"{BASE}/{sid_}/blocks/all", {"referent_id": "nope"})[0] == 404
    assert call(srv, "POST", f"{BASE}/{sid_}/blocks/all", {})[0] == 400
    status, body = call(srv, "DELETE", f"{BASE}/{sid_}/blocks")
    assert status == 200 and body["entries"] == [] and body["can_undo"]
    assert call(srv, "POST", f"{BASE}/{sid_}/undo", {})[0] == 200
    assert len(call(srv, "GET", f"{BASE}/{sid_}")[1]["entries"]) == len(blocks["items"])


def test_full_editing_flow_over_http(srv):
    _, created = call(srv, "POST", BASE, {"name": "For Coastal Residency"})
    sid_ = created["id"]
    _, blocks = call(srv, "GET", "/api/cv-referents/short/blocks")
    b1, b2 = blocks["items"][0], blocks["items"][1]
    status, body = call(srv, "POST", f"{BASE}/{sid_}/blocks", {"referent_id": "short", "block_id": b1["id"]})
    assert status == 200 and [e["id"] for e in body["entries"]] == [b1["id"]] and body["can_undo"]
    assert call(srv, "POST", f"{BASE}/{sid_}/blocks", {"referent_id": "short", "block_id": b1["id"]})[0] == 409
    call(srv, "POST", f"{BASE}/{sid_}/blocks", {"referent_id": "short", "block_id": b2["id"]})
    status, body = call(srv, "PUT", f"{BASE}/{sid_}/blocks/{b1['id']}", {"text": "Changed"})
    assert status == 200 and body["entries"][0]["text"] == "Changed"
    assert call(srv, "GET", "/api/cv-referents/short/blocks")[1]["items"][0]["text"] == b1["text"]
    # b1 is a heading and b2 an entry: they hold different places, so swapping them is refused,
    # and the order as it is answers 200 without recording a step.
    assert call(srv, "PUT", f"{BASE}/{sid_}/order", {"block_ids": [b2["id"], b1["id"]]})[0] == 400
    status, body = call(srv, "PUT", f"{BASE}/{sid_}/order", {"block_ids": [b1["id"], b2["id"]]})
    assert status == 200 and [e["id"] for e in body["entries"]] == [b1["id"], b2["id"]]
    status, body = call(srv, "DELETE", f"{BASE}/{sid_}/blocks/{b2['id']}")
    assert status == 200 and body["entries"] == []                      # b1 is b2's heading: it goes with its last entry
    status, prev = call(srv, "GET", f"{BASE}/{sid_}/preview")
    assert status == 200 and prev["page_count"] == 0 and prev["pages_origin"] == "mock"
    for _ in range(3):                                     # delete, edit, add b2 (add b1 stays) -> undo three
        assert call(srv, "POST", f"{BASE}/{sid_}/undo", {})[0] == 200
    _, body = call(srv, "GET", f"{BASE}/{sid_}")
    assert [e["id"] for e in body["entries"]] == [b1["id"]] and body["entries"][0]["text"] == b1["text"]
    status, body = call(srv, "POST", f"{BASE}/{sid_}/save", {"name": "Saved CV"})
    assert status == 200 and body["saved"]
    _, refs = call(srv, "GET", "/api/cv-referents")
    assert "Saved CV" in [i["name"] for i in refs["items"]]
    assert call(srv, "POST", f"{BASE}/{sid_}/archive", {})[0] == 200
    assert call(srv, "PUT", f"{BASE}/{sid_}/blocks/{b1['id']}", {"text": "no"})[0] == 409
    assert call(srv, "POST", f"{BASE}/{sid_}/unarchive", {})[0] == 200
    assert call(srv, "DELETE", f"{BASE}/{sid_}")[0] == 200
    assert call(srv, "GET", f"{BASE}/{sid_}")[0] == 404


def test_bad_bodies_over_http(srv):
    _, created = call(srv, "POST", BASE, {})
    sid_ = created["id"]
    bid = call(srv, "GET", "/api/cv-referents/full/blocks")[1]["items"][0]["id"]
    assert call(srv, "POST", f"{BASE}/{sid_}/blocks", {"block_id": bid})[0] == 400
    assert call(srv, "POST", f"{BASE}/{sid_}/blocks", {"referent_id": "full", "block_id": "x"})[0] == 400
    assert call(srv, "POST", f"{BASE}/{sid_}/blocks", {"referent_id": "full", "block_id": str(uuid.uuid4())})[0] == 404
    assert call(srv, "POST", f"{BASE}/{sid_}/blocks", {"referent_id": "nope", "block_id": bid})[0] == 404
    assert call(srv, "POST", f"{BASE}/{sid_}/blocks", {"referent_id": "full", "block_id": bid}, headers={})[0] == 415
    call(srv, "POST", f"{BASE}/{sid_}/blocks", {"referent_id": "full", "block_id": bid})
    assert call(srv, "PUT", f"{BASE}/{sid_}/blocks/{bid}", {"text": ""})[0] == 400
    assert call(srv, "PUT", f"{BASE}/{sid_}/blocks/{bid}", {})[0] == 400
    assert call(srv, "PUT", f"{BASE}/{sid_}/blocks/{uuid.uuid4()}", {"text": "x"})[0] == 404
    assert call(srv, "PUT", f"{BASE}/{sid_}/order", {"block_ids": []})[0] == 400
    assert call(srv, "POST", f"{BASE}/{sid_}/undo", {})[0] == 200
    assert call(srv, "POST", f"{BASE}/{sid_}/undo", {})[0] == 409
    # Portfolio-only and CV-only routes do not cross over.
    assert call(srv, "POST", f"/api/sets/portfolio/{sid_}/blocks", {"referent_id": "full", "block_id": bid})[0] == 404
    assert call(srv, "POST", f"{BASE}/{sid_}/entries", {})[0] == 404


# --- where an added block lands: its section, newest year first -------------------------------

def B(kind, text, n):
    return {"id": str(uuid.UUID(int=n)), "kind": kind, "text": text}


REF = [
    B("heading", "EDUCATION", 1), B("text", "2024\tBAI", 2), B("text", "2021\tMA", 3), B("text", "2015\tAA", 4),
    B("heading", "SOLO", 5), B("text", "2024\tSolo A", 6), B("text", "2022\tSolo B", 7), B("text", "2022\tSolo C", 8),
    B("heading", "GROUP", 9), B("text", "2025\tGroup A", 10), B("text", "2023\tGroup B", 11), B("text", "2019\tGroup C", 12),
    B("heading", "GRANTS", 13), B("text", "2024\tGrant", 14), B("text", "no year here", 15),
]
by = {b["id"]: b for b in REF}


def tx(content):
    return [b["text"] for b in content]


def place(content, n):
    return cv_sets._place(content, by[str(uuid.UUID(int=n))], REF)


def test_an_added_entry_goes_into_its_section_in_year_order():
    content = [REF[8], REF[9], REF[11]]                      # GROUP: 2025, 2019
    assert tx(place(content, 11)) == ["GROUP", "2025\tGroup A", "2023\tGroup B", "2019\tGroup C"]
    newest = place([REF[8], REF[10]], 10)
    assert tx(newest) == ["GROUP", "2025\tGroup A", "2023\tGroup B"]
    oldest = place([REF[8], REF[9]], 12)
    assert tx(oldest) == ["GROUP", "2025\tGroup A", "2019\tGroup C"]


def test_it_stays_inside_its_own_section_even_when_later_sections_follow():
    content = [REF[0], REF[1], REF[8], REF[9], REF[12], REF[13]]      # EDUCATION, GROUP, GRANTS
    out = tx(place(content, 11))
    assert out == ["EDUCATION", "2024\tBAI", "GROUP", "2025\tGroup A", "2023\tGroup B", "GRANTS", "2024\tGrant"]


def test_the_same_year_goes_after_the_ones_already_there_and_undated_last():
    content = [REF[4], REF[6]]                                # SOLO: 2022 Solo B
    assert tx(place(content, 8)) == ["SOLO", "2022\tSolo B", "2022\tSolo C"]
    assert tx(place([REF[12], REF[13]], 15)) == ["GRANTS", "2024\tGrant", "no year here"]
    assert tx(place([REF[12], REF[14]], 14)) == ["GRANTS", "2024\tGrant", "no year here"]    # dated before undated


def test_a_missing_section_is_created_in_the_referents_section_order():
    content = [REF[0], REF[1], REF[12], REF[13]]              # EDUCATION, GRANTS
    out = tx(place(content, 6))                            # SOLO comes between them
    assert out == ["EDUCATION", "2024\tBAI", "SOLO", "2024\tSolo A", "GRANTS", "2024\tGrant"]
    assert tx(place([], 10)) == ["GROUP", "2025\tGroup A"]
    assert tx(place([REF[0], REF[1]], 10))[-2:] == ["GROUP", "2025\tGroup A"]          # after the last one


def test_adding_a_heading_alone_and_a_block_without_a_heading():
    assert tx(place([REF[0], REF[12]], 5)) == ["EDUCATION", "SOLO", "GRANTS"]
    orphan = B("text", "no section", 99)
    assert cv_sets._place([REF[0]], orphan, [orphan]) == [REF[0], orphan]


def test_deleting_the_last_entry_of_a_section_takes_its_heading_along_and_undo_brings_both_back(db, cv):
    target = new_set(db)
    entries = [b for b in cv.get_blocks("full") if b.kind == "text"][:2]
    for b in entries:
        add(db, cv, target, "full", b.id)
    before = get(db, target).content
    kinds = [b["kind"] for b in before]
    with db.tx() as c:
        rec = cv_sets.delete_block(c, target, entries[0].id)
    # another entry still under the same heading: the heading stays
    if kinds == ["heading", "text", "text"]:
        assert [b["kind"] for b in rec.content] == ["heading", "text"]
        with db.tx() as c:
            rec = cv_sets.delete_block(c, target, entries[1].id)
        assert rec.content == []                         # last entry: the heading goes too
    with db.tx() as c:
        assert store.undo_last(c, "cv", target).content is not None


def test_delete_only_removes_the_heading_directly_above_an_emptied_section(db):
    """Two sections: emptying the second takes its heading, the first section is untouched."""
    set_id = new_set(db)
    blocks = [B("heading", "A", 1), B("text", "2024\tx", 2), B("heading", "B", 3), B("text", "2023\ty", 4)]
    with db.tx() as c:
        store.mutate(c, "cv", set_id, lambda content: blocks)
        rec = cv_sets.delete_block(c, set_id, blocks[3]["id"])
    assert [b["text"] for b in rec.content] == ["A", "2024\tx"]
    with db.tx() as c:
        rec = cv_sets.delete_block(c, set_id, blocks[1]["id"])
    assert rec.content == []
    # a heading that had no entries to begin with is not swept up by an unrelated delete
    keep = [B("heading", "Empty", 5), B("heading", "C", 6), B("text", "2022\tz", 7)]
    with db.tx() as c:
        store.mutate(c, "cv", set_id, lambda content: keep)
        rec = cv_sets.delete_block(c, set_id, keep[2]["id"])
    assert [b["text"] for b in rec.content] == ["Empty"]
