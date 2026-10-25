"""Sets: shared behaviour (store, undo), portfolio content, and the endpoints."""

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from core.config import Config
from core.db import Database
from core.errors import Conflict, NotFound
from core.seed import seed_opportunities, seed_portfolio_sets
from models.set import UNDO_LIMIT
from sets import portfolio_sets, store, undo
from sources.local_projects import LocalProjects
from web import server as s

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"
T = "2026-09-20T12:00:00Z"
JSON = {"Content-Type": "application/json"}


@pytest.fixture
def source():
    return LocalProjects(ROOT / "tests" / "fixtures" / "projects")


@pytest.fixture
def db(tmp_path, source):
    d = Database(tmp_path / "arta.sqlite3")
    d.migrate()
    seed_opportunities(d, FIXTURES)
    seed_portfolio_sets(d, FIXTURES, source)
    return d


@pytest.fixture
def project(source):
    p = next(p for p in source.list_projects() if p.name == "Field Notes")
    return (p, [i.id for i in p.images if i.role == "work"], [i.id for i in p.images if i.role == "exhibition"])


def names(db):
    with db.read() as c:
        return [r.name for r in store.list_sets(c, "portfolio")]


def set_id(db, name):
    with db.read() as c:
        return next(r.id for r in store.list_sets(c, "portfolio") if r.name == name)


def entry(project, works=(0,), shows=(), amount="short"):
    p, w, e = project
    return {"project_id": p.id, "works": [w[i] for i in works], "exhibitions": [e[i] for i in shows], "text_amount": amount}


# --- the sidebar order (spec 5.3) -------------------------------------------

def test_seed_covers_the_four_sidebar_groups_in_order(db):
    assert names(db) == ["New proposed set", "Portfolio Set 1", "Harbour overview",
                         "Saved by Jane set", "Wexford application", "Old proposal"]


def test_seed_only_fills_an_empty_table(db, source):
    assert seed_portfolio_sets(db, FIXTURES, source) == 0


def test_saving_does_not_move_a_set(db):
    before = names(db)
    sid = set_id(db, "Saved by Jane set")
    with db.tx() as c:
        store.save(c, "portfolio", sid, "Renamed", T)
        store.set_saved(c, "portfolio", set_id(db, "New proposed set"), True, T)
    assert names(db) == ["Renamed" if n == "Saved by Jane set" else n for n in before]


def test_a_new_set_opens_the_sidebar_and_the_users_order_is_kept(db):
    with db.tx() as c:
        made = store.create(c, "portfolio", T, "Fresh")
    assert names(db)[0] == "Fresh"
    with db.read() as c:
        shown = [r.id for r in store.list_sets(c, "portfolio") if not r.archived]
    mine = shown[::-1]
    with db.tx() as c:
        store.reorder_sidebar(c, "portfolio", mine)
        store.set_saved(c, "portfolio", made.id, True, T)           # saving still moves nothing
    with db.read() as c:
        assert [r.id for r in store.list_sets(c, "portfolio") if not r.archived] == mine
        assert store.list_sets(c, "portfolio")[-1].archived          # archived sets follow the list


def test_sidebar_order_must_name_every_shown_set_once(db):
    with db.read() as c:
        shown = [r.id for r in store.list_sets(c, "portfolio") if not r.archived]
    with db.tx() as c:
        for bad in (shown[:-1], shown + [shown[0]], shown + ["nope"], "x"):
            with pytest.raises(ValueError):
                store.reorder_sidebar(c, "portfolio", bad)


def test_a_set_whose_opportunity_is_applied_has_its_own_group_and_survives_unsaving(db):
    sid = set_id(db, "Wexford application")
    with db.tx() as c:
        store.set_saved(c, "portfolio", sid, False, T)
    assert names(db).index("Wexford application") == 4          # still after the saved sets


# --- naming -----------------------------------------------------------------

def test_generated_portfolio_name_is_an_adjective_not_used_by_any_set_archived_included(db):
    with db.tx() as c:
        taken = {f"{a} portfolio set" for a in store.ADJECTIVES}
        free = store.ADJECTIVES[0]
        for a in store.ADJECTIVES[1:]:
            made = store.create(c, "portfolio", T, f"{a} portfolio set")
        store.archive(c, "portfolio", made.id, T)                     # archived names stay taken
        assert store.create(c, "portfolio", T).name == f"{free} portfolio set"
        assert store.create(c, "portfolio", T).name not in taken      # all taken: a number is added
        assert store.create(c, "portfolio", T).name.split()[-1].isdigit()


def test_generated_names_look_like_adjective_portfolio_set(db):
    with db.tx() as c:
        for _ in range(5):
            adjective, rest = store.create(c, "portfolio", T).name.split(" ", 1)
            assert adjective in store.ADJECTIVES and rest == "portfolio set"


def test_cv_sets_keep_numbered_names(db):
    with db.tx() as c:
        assert store.create(c, "cv", T).name.startswith("CV Set ")


def test_first_generated_name_and_bad_names(tmp_path):
    d = Database(tmp_path / "a.sqlite3")
    d.migrate()
    with d.tx() as c:
        assert store.create(c, "portfolio", T).name.endswith(" portfolio set")
        with pytest.raises(ValueError):
            store.create(c, "portfolio", T, "   ")
        with pytest.raises(ValueError):
            store.create(c, "portfolio", T, "x" * 121)
        assert store.create(c, "portfolio", T, "<b>Bold</b> set").name == "Bold set"


# --- save / archive / delete -------------------------------------------------

def test_archive_is_final_for_edits_and_not_repeatable(db, project, source):
    sid = set_id(db, "Old proposal")
    with db.tx() as c:
        with pytest.raises(Conflict):
            store.archive(c, "portfolio", sid, T)
        with pytest.raises(Conflict):
            portfolio_sets.add_entry(c, source, sid, entry(project))
        with pytest.raises(Conflict):
            store.save(c, "portfolio", sid, "x", T)


def test_unarchive_restores_the_set_exactly_and_it_is_editable_again(db, project, source):
    sid = set_id(db, "Harbour overview")
    before_names = names(db)
    with db.tx() as c:
        portfolio_sets.add_entry(c, source, sid, entry(project))          # one undo step
        before = store.get(c, "portfolio", sid)
        store.archive(c, "portfolio", sid, T)
    assert names(db) == before_names                                  # archiving moves nothing
    with db.tx() as c:
        after = store.unarchive(c, "portfolio", sid)
        assert (after.name, after.saved, after.saved_at, after.content, after.can_undo) == (
            before.name, before.saved, before.saved_at, before.content, True)
        assert not after.archived and after.archived_at is None
        portfolio_sets.add_entry(c, source, sid, entry(project))          # editable again
        with pytest.raises(Conflict):
            store.unarchive(c, "portfolio", sid)                           # not archived any more
    assert names(db) == ["New proposed set", "Portfolio Set 1", "Harbour overview",
                         "Saved by Jane set", "Wexford application", "Old proposal"]     # back in its own group


def test_delete_removes_the_set_and_its_undo_steps(db, project, source):
    sid = set_id(db, "New proposed set")
    with db.tx() as c:
        portfolio_sets.add_entry(c, source, sid, entry(project))
        assert c.execute("SELECT COUNT(*) FROM set_undo WHERE set_id = ?", (sid,)).fetchone()[0] == 1
        store.delete(c, "portfolio", sid)
        assert c.execute("SELECT COUNT(*) FROM set_undo").fetchone()[0] == 0
        with pytest.raises(NotFound):
            store.get(c, "portfolio", sid)


def test_a_set_is_only_reachable_as_its_own_kind(db):
    sid = set_id(db, "New proposed set")
    with db.read() as c:
        with pytest.raises(NotFound):
            store.get(c, "cv", sid)
        with pytest.raises(NotFound):
            store.get(c, "bogus", sid)


# --- undo -------------------------------------------------------------------

def test_undo_keeps_twenty_steps_and_restores_exactly(db, project, source):
    sid = set_id(db, "Portfolio Set 1")
    states = []
    with db.tx() as c:
        states.append(store.get(c, "portfolio", sid).content)
        for _ in range(25):
            states.append(portfolio_sets.add_entry(c, source, sid, entry(project)).content)
    with db.tx() as c:
        for i in range(UNDO_LIMIT):
            rec = store.undo_last(c, "portfolio", sid)
            assert rec.content == states[25 - 1 - i]          # each step back, exactly
        with pytest.raises(Conflict):
            store.undo_last(c, "portfolio", sid)              # step 21 was dropped
    assert len(states[25 - UNDO_LIMIT]) == 5                  # 25 added, 20 undone


def test_redo_replays_undone_steps_and_a_new_change_ends_the_line(db, project, source):
    sid = set_id(db, "Portfolio Set 1")
    with db.tx() as c:
        first = portfolio_sets.add_entry(c, source, sid, entry(project)).content
        second = portfolio_sets.add_entry(c, source, sid, entry(project)).content
    with db.tx() as c:
        assert not store.get(c, "portfolio", sid).can_redo
        with pytest.raises(Conflict):
            store.redo_last(c, "portfolio", sid)              # nothing undone yet
        store.undo_last(c, "portfolio", sid)
        store.undo_last(c, "portfolio", sid)
        assert store.get(c, "portfolio", sid).can_redo
        assert store.redo_last(c, "portfolio", sid).content == first
        rec = store.redo_last(c, "portfolio", sid)
        assert rec.content == second and not rec.can_redo and rec.can_undo
        with pytest.raises(Conflict):
            store.redo_last(c, "portfolio", sid)
        store.undo_last(c, "portfolio", sid)
        rec = portfolio_sets.add_entry(c, source, sid, entry(project))
        assert not rec.can_redo                               # a new change clears redo
        store.undo_last(c, "portfolio", sid)
        store.reset(c, "portfolio", sid)
        assert not store.get(c, "portfolio", sid).can_redo    # reset clears both stacks


def test_undo_survives_a_restart(db, project, source):
    sid = set_id(db, "Portfolio Set 1")
    with db.tx() as c:
        portfolio_sets.add_entry(c, source, sid, entry(project))
    reopened = Database(db.path)                              # a new process, the same file
    with reopened.tx() as c:
        assert store.get(c, "portfolio", sid).can_undo
        assert store.undo_last(c, "portfolio", sid).content == []


def test_a_change_that_changes_nothing_is_not_an_undo_step(db):
    sid = set_id(db, "New proposed set")
    with db.tx() as c:
        rec = store.mutate(c, "portfolio", sid, lambda content: content)
        assert not rec.can_undo


def test_undo_stacks_are_per_set(db, project, source):
    a, b = set_id(db, "Portfolio Set 1"), set_id(db, "New proposed set")
    with db.tx() as c:
        portfolio_sets.add_entry(c, source, a, entry(project))
        assert store.get(c, "portfolio", a).can_undo and not store.get(c, "portfolio", b).can_undo
        with pytest.raises(Conflict):
            store.undo_last(c, "portfolio", b)


def test_undo_stack_helpers_are_content_agnostic(db):
    sid = set_id(db, "Portfolio Set 1")
    with db.tx() as c:
        undo.push(c, sid, {"anything": [1, 2]})
        assert undo.pop(c, sid) == {"anything": [1, 2]} and undo.pop(c, sid) is None


# --- reset ------------------------------------------------------------------

def test_reset_returns_to_the_saved_content_and_is_not_undoable(db, project, source):
    sid = set_id(db, "Harbour overview")
    with db.tx() as c:
        saved = store.get(c, "portfolio", sid).content
        portfolio_sets.add_entry(c, source, sid, entry(project))
        portfolio_sets.delete_entry(c, sid, saved[0]["id"])
        rec = store.reset(c, "portfolio", sid)
        assert rec.content == saved and not rec.can_undo
        with pytest.raises(Conflict):
            store.undo_last(c, "portfolio", sid)


def test_reset_of_a_never_saved_set_empties_it(db, project, source):
    sid = set_id(db, "New proposed set")
    with db.tx() as c:
        assert store.reset(c, "portfolio", sid).content == []


def test_saving_moves_the_reset_point(db, project, source):
    sid = set_id(db, "Portfolio Set 1")
    with db.tx() as c:
        portfolio_sets.add_entry(c, source, sid, entry(project))
        store.save(c, "portfolio", sid, "Kept", T)
        portfolio_sets.add_entry(c, source, sid, entry(project))
        assert len(store.reset(c, "portfolio", sid).content) == 1


# --- portfolio content ------------------------------------------------------

def test_pick_order_is_kept(db, project, source):
    sid = set_id(db, "Portfolio Set 1")
    with db.tx() as c:
        rec = portfolio_sets.add_entry(c, source, sid, entry(project, works=(3, 1, 2), shows=(4, 0)))
    e = rec.content[0]
    _, works, shows = project
    assert e["works"] == [works[3], works[1], works[2]] and e["exhibitions"] == [shows[4], shows[0]]


@pytest.mark.parametrize("bad", [
    {},
    {"project_id": ""},
    {"project_id": "P", "text_amount": "huge"},
    {"project_id": "P", "works": "x"},
    {"project_id": "P", "works": ["nope"]},
    {"project_id": "P", "extra": 1},
])
def test_entry_validation_rejects_bad_bodies(db, project, source, bad):
    sid = set_id(db, "Portfolio Set 1")
    body = {**bad, **({"project_id": project[0].id} if bad.get("project_id") == "P" else {})}
    with db.tx() as c:
        with pytest.raises(ValueError):
            portfolio_sets.add_entry(c, source, sid, body)


def test_works_and_exhibitions_cannot_swap_or_repeat_and_projects_must_exist(db, project, source):
    p, works, shows = project
    sid = set_id(db, "Portfolio Set 1")
    with db.tx() as c:
        for body in ({"project_id": p.id, "works": [shows[0]]},          # an exhibition image as a work
                     {"project_id": p.id, "exhibitions": [works[0]]},
                     {"project_id": p.id, "works": [works[0], works[0]]}):
            with pytest.raises(ValueError):
                portfolio_sets.add_entry(c, source, sid, body)
        with pytest.raises(NotFound):
            portfolio_sets.add_entry(c, source, sid, {"project_id": "nope"})
        assert store.get(c, "portfolio", sid).content == []              # nothing half-written


def test_edit_delete_reorder_are_each_one_undo_step(db, project, source):
    sid = set_id(db, "Harbour overview")
    with db.tx() as c:
        before = store.get(c, "portfolio", sid).content
        a, b = before[0]["id"], before[1]["id"]
        edited = portfolio_sets.edit_entry(c, source, sid, a, entry(project, works=(2,), amount="full"))
        assert edited.content[0]["id"] == a and edited.content[0]["works"] == [project[1][2]]
        assert portfolio_sets.reorder(c, sid, [b, a]).content[0]["id"] == b
        assert [e["id"] for e in portfolio_sets.delete_entry(c, sid, b).content] == [a]
        for _ in range(3):
            store.undo_last(c, "portfolio", sid)
        assert store.get(c, "portfolio", sid).content == before
        with pytest.raises(NotFound):
            portfolio_sets.delete_entry(c, sid, "nope")
        with pytest.raises(NotFound):
            portfolio_sets.edit_entry(c, source, sid, "nope", entry(project))
        for bad in ([a], [a, a], [a, "x"], "ab", None):
            with pytest.raises(ValueError):
                portfolio_sets.reorder(c, sid, bad)


def test_describe_names_projects_counts_images_and_marks_pages_as_mock(db, project, source):
    sid = set_id(db, "Harbour overview")
    with db.read() as c:
        rec = store.get(c, "portfolio", sid)
        d = portfolio_sets.describe(rec, source)
    assert [e["project_name"] for e in d["entries"]] == ["Field Notes", "Field Notes"]
    assert [e["image_count"] for e in d["entries"]] == [7, 1]
    assert d["pages"] == portfolio_sets.preview(rec, source)["page_count"] and d["pages_origin"] == "mock"


def test_preview_is_one_sheet_per_page_and_every_entry_opens_with_an_opening_sheet(db, project, source):
    sid = set_id(db, "Portfolio Set 1")
    with db.tx() as c:
        for _ in range(3):
            rec = portfolio_sets.add_entry(c, source, sid, entry(project))
    p = portfolio_sets.preview(rec, source)
    assert p["pages_origin"] == "mock" and p["page_count"] == len(p["pages"]) >= 3
    firsts = [s["layout"] for n, s in enumerate(p["pages"]) if n == 0 or s["entry_id"] != p["pages"][n - 1]["entry_id"]]
    assert len(firsts) == 3 and set(firsts) <= {"band", "side", "figure-side", "grid-side"}
    assert [e["id"] for e in rec.content] == list(dict.fromkeys(s["entry_id"] for s in p["pages"]))


def test_an_entry_whose_project_disappeared_is_flagged_not_dropped(db, project, source, tmp_path):
    sid = set_id(db, "Harbour overview")
    with db.read() as c:
        rec = store.get(c, "portfolio", sid)
    (tmp_path / "empty").mkdir()                 # the folder is there, the project is not
    d = portfolio_sets.describe(rec, LocalProjects(tmp_path / "empty"))
    assert all(e["project_missing"] and e["project_name"] is None for e in d["entries"])


# --- HTTP -------------------------------------------------------------------

@pytest.fixture
def srv(db, tmp_path, source):
    cfg = Config(updates="fixtures", projects="fixtures", cv="fixtures", llm="mock", port=0, data_dir=tmp_path / "data")
    server = s.make_server(cfg, db, port=0, projects=source)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()
    server.server_close()


def call(srv, method, path, body=None, headers=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{srv.server_address[1]}{path}", method=method, data=data,
                                 headers=headers if headers is not None else (JSON if data else {}))
    if not req.has_header("Origin"):   # a browser sends it on every write
        req.add_header("Origin", "http://" + req.host)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


BASE = "/api/sets/portfolio"


def test_list_get_create(srv):
    status, body = call(srv, "GET", BASE)
    assert status == 200 and [i["name"] for i in body["items"]][0] == "New proposed set"
    assert body["items"][0]["entry_count"] == 1 and "entries" not in body["items"][0]
    sid = body["items"][2]["id"]
    status, one = call(srv, "GET", f"{BASE}/{sid}")
    assert status == 200 and one["name"] == "Harbour overview" and one["pages_origin"] == "mock"
    assert one["entries"][0]["project_name"] == "Field Notes"
    status, made = call(srv, "POST", BASE, {})
    assert status == 201 and made["name"].endswith(" portfolio set") and made["entries"] == []
    assert call(srv, "GET", f"{BASE}/nope")[0] == 404
    assert call(srv, "POST", BASE, {"name": "  "})[0] == 400


def test_new_full_set_holds_every_project_with_all_pictures_and_full_text(srv, source):
    status, made = call(srv, "POST", f"{BASE}/full", {})
    assert status == 201 and made["name"].endswith(" portfolio set") and not made["can_undo"]
    projects = source.list_projects()
    assert [e["project_id"] for e in made["entries"]] == [p.id for p in projects]
    for entry, project in zip(made["entries"], projects):
        assert entry["works"] == [i.id for i in project.images if i.role == "work"]
        assert entry["exhibitions"] == [i.id for i in project.images if i.role == "exhibition"]
        assert entry["text_amount"] == "full"
    assert len({e["id"] for e in made["entries"]}) == len(projects)
    assert call(srv, "GET", BASE)[1]["items"][0]["id"] == made["id"]      # opens the sidebar
    _, shown = call(srv, "GET", f"{BASE}/{made['id']}/preview")
    assert shown["page_count"] == len(shown["pages"]) > len(projects)


def test_full_editing_flow_with_undo_and_reset_over_http(srv):
    _, made = call(srv, "POST", BASE, {"name": "Flow"})
    sid = made["id"]
    _, projects = call(srv, "GET", "/api/projects")
    pid = projects["items"][0]["id"]
    _, detail = call(srv, "GET", f"/api/projects/{pid}")
    works = [i["id"] for i in detail["images"] if i["role"] == "work"]
    status, added = call(srv, "POST", f"{BASE}/{sid}/entries",
                         {"project_id": pid, "works": [works[2], works[0]], "text_amount": "middle"})
    assert status == 200 and added["entries"][0]["works"] == [works[2], works[0]] and added["can_undo"]
    eid = added["entries"][0]["id"]
    status, edited = call(srv, "PUT", f"{BASE}/{sid}/entries/{eid}", {"project_id": pid, "works": [works[1]]})
    assert status == 200 and edited["entries"][0]["id"] == eid and edited["entries"][0]["text_amount"] == "short"
    assert call(srv, "POST", f"{BASE}/{sid}/undo", {})[1]["entries"][0]["works"] == [works[2], works[0]]
    _, shown = call(srv, "GET", f"{BASE}/{sid}/preview")
    assert shown["pages_origin"] == "mock" and shown["page_count"] == len(shown["pages"])
    assert {s["entry_id"] for s in shown["pages"]} == {eid} and shown["pages"][0]["layout"] in ("band", "side", "figure-side")
    _, saved = call(srv, "POST", f"{BASE}/{sid}/save", {"name": "Kept"})
    assert saved["saved"] and saved["name"] == "Kept"
    call(srv, "DELETE", f"{BASE}/{sid}/entries/{eid}")
    _, reset = call(srv, "POST", f"{BASE}/{sid}/reset", {})
    assert len(reset["entries"]) == 1 and not reset["can_undo"]
    assert call(srv, "POST", f"{BASE}/{sid}/undo", {})[0] == 409


def test_bulk_delete_removes_the_chosen_projects_in_one_undo_step(srv, source):
    _, made = call(srv, "POST", f"{BASE}/full", {})
    sid, ids = made["id"], [e["id"] for e in made["entries"]]
    assert len(ids) >= 2
    status, out = call(srv, "POST", f"{BASE}/{sid}/entries/delete", {"entry_ids": ids[:2]})
    assert status == 200 and [e["id"] for e in out["entries"]] == ids[2:] and out["can_undo"]
    back = call(srv, "POST", f"{BASE}/{sid}/undo", {})[1]
    assert [e["id"] for e in back["entries"]] == ids                    # one Undo, all of them
    assert call(srv, "POST", f"{BASE}/{sid}/entries/delete", {"entry_ids": [ids[0], "nope"]})[0] == 404
    assert [e["id"] for e in call(srv, "GET", f"{BASE}/{sid}")[1]["entries"]] == ids   # nothing on a miss
    assert call(srv, "POST", f"{BASE}/{sid}/entries/delete", {"entry_ids": []})[0] == 400
    assert call(srv, "POST", f"{BASE}/{sid}/entries/delete", {})[0] == 400


def test_order_patch_archive_delete_over_http(srv):
    items = call(srv, "GET", BASE)[1]["items"]
    sid = next(i["id"] for i in items if i["name"] == "Harbour overview")
    entries = call(srv, "GET", f"{BASE}/{sid}")[1]["entries"]
    ids = [e["id"] for e in entries][::-1]
    assert [e["id"] for e in call(srv, "PUT", f"{BASE}/{sid}/order", {"entry_ids": ids})[1]["entries"]] == ids
    assert call(srv, "PUT", f"{BASE}/{sid}/order", {"entry_ids": ids[:1]})[0] == 400
    assert call(srv, "PATCH", f"{BASE}/{sid}", {"saved": False})[1]["saved"] is False
    assert call(srv, "PATCH", f"{BASE}/{sid}", {"saved": True, "name": "x"})[0] == 400
    assert call(srv, "PATCH", f"{BASE}/{sid}", {"other": 1})[0] == 400
    assert call(srv, "PATCH", f"{BASE}/{sid}", {"saved": "yes"})[0] == 400
    assert call(srv, "POST", f"{BASE}/{sid}/archive", {})[1]["archived"] is True
    assert call(srv, "POST", f"{BASE}/{sid}/archive", {})[0] == 409
    assert call(srv, "POST", f"{BASE}/{sid}/entries", {"project_id": "x"})[0] == 409     # archived: read-only
    back = call(srv, "POST", f"{BASE}/{sid}/unarchive", {})
    assert back[0] == 200 and back[1]["archived"] is False and back[1]["archived_at"] is None
    assert call(srv, "POST", f"{BASE}/{sid}/unarchive", {})[0] == 409
    assert call(srv, "POST", f"{BASE}/{sid}/archive", {})[0] == 200
    items = call(srv, "GET", BASE)[1]["items"]
    assert [i["archived"] for i in items].count(True) == 2                                  # the page hides them
    assert [i["id"] for i in items].index(sid) == 2                                         # archiving moved nothing
    assert call(srv, "DELETE", f"{BASE}/{sid}") == (200, {"deleted": sid})
    assert call(srv, "GET", f"{BASE}/{sid}")[0] == 404


def test_sidebar_order_over_http(srv):
    items = call(srv, "GET", BASE)[1]["items"]
    shown = [i["id"] for i in items if not i["archived"]]
    flipped = shown[::-1]
    status, answer = call(srv, "PUT", f"{BASE}/order", {"ids": flipped})
    assert status == 200 and [i["id"] for i in answer["items"] if not i["archived"]] == flipped
    assert [i["id"] for i in call(srv, "GET", BASE)[1]["items"] if not i["archived"]] == flipped   # kept
    assert call(srv, "PUT", f"{BASE}/order", {"ids": flipped[:-1]})[0] == 400                    # stale list
    assert call(srv, "PUT", f"{BASE}/order", {})[0] == 400


def test_writes_need_json_and_unknown_project_is_404(srv):
    sid = call(srv, "GET", BASE)[1]["items"][1]["id"]
    assert call(srv, "POST", f"{BASE}/{sid}/undo", {}, headers={})[0] == 415
    assert call(srv, "POST", f"{BASE}/{sid}/entries", {"project_id": "nope"})[0] == 404
    assert call(srv, "POST", f"{BASE}/{sid}/entries", [1])[0] == 400


# --- rename and duplicate ---------------------------------------------------

def test_rename_changes_only_the_name(db):
    sid = set_id(db, "Harbour overview")
    with db.read() as c:
        before = store.get(c, "portfolio", sid)
    with db.tx() as c:
        after = store.rename(c, "portfolio", sid, "  A <b>new</b> name ")
    assert after.name == "A new name"
    assert (after.saved, after.saved_at, after.content, after.can_undo) == (before.saved, before.saved_at, before.content, before.can_undo)


def test_rename_is_refused_for_empty_names_and_archived_sets(db):
    sid = set_id(db, "Harbour overview")
    with db.tx() as c:
        with pytest.raises(ValueError):
            store.rename(c, "portfolio", sid, "   ")
        store.archive(c, "portfolio", sid, T)
        with pytest.raises(Conflict):
            store.rename(c, "portfolio", sid, "x")


def test_rename_of_an_unsaved_set_does_not_save_it(db):
    sid = set_id(db, "New proposed set")
    with db.tx() as c:
        assert store.rename(c, "portfolio", sid, "Renamed").saved is False


def test_duplicate_copies_content_into_a_new_saved_set(db, project, source):
    sid = set_id(db, "Portfolio Set 1")
    with db.tx() as c:
        portfolio_sets.add_entry(c, source, sid, entry(project))
        original = store.get(c, "portfolio", sid)
        copy = store.duplicate(c, "portfolio", sid, "Second pass", T)
        assert copy.id != original.id and copy.name == "Second pass"
        assert copy.content == original.content and copy.content
        assert copy.saved and copy.saved_at == T and not copy.archived and not copy.can_undo
        assert copy.opportunity_id is None
        store.reset(c, "portfolio", copy.id)                     # baseline is the copied content
        assert store.get(c, "portfolio", copy.id).content == original.content
        assert store.get(c, "portfolio", sid) == original


def test_duplicate_is_independent_of_the_original(db, project, source):
    sid = set_id(db, "Portfolio Set 1")
    with db.tx() as c:
        copy = store.duplicate(c, "portfolio", sid, "Copy", T)
        portfolio_sets.add_entry(c, source, copy.id, entry(project))
        assert store.get(c, "portfolio", sid).content == []


def test_duplicate_needs_a_name_and_works_on_archived_sets(db):
    sid = set_id(db, "Old proposal")
    with db.tx() as c:
        with pytest.raises(ValueError):
            store.duplicate(c, "portfolio", sid, "", T)
        assert store.get(c, "portfolio", sid).archived
        assert not store.duplicate(c, "portfolio", sid, "Revived", T).archived


def test_rename_and_duplicate_over_http(srv):
    items = call(srv, "GET", BASE)[1]["items"]
    sid = next(i["id"] for i in items if i["name"] == "Portfolio Set 1")
    status, renamed = call(srv, "PATCH", f"{BASE}/{sid}", {"name": "Fresh name"})
    assert status == 200 and renamed["name"] == "Fresh name" and renamed["saved"] is False
    assert call(srv, "PATCH", f"{BASE}/{sid}", {"name": ""})[0] == 400
    status, copy = call(srv, "POST", f"{BASE}/{sid}/duplicate", {"name": "Fresh name copy"})
    assert status == 201 and copy["name"] == "Fresh name copy" and copy["saved"] and copy["id"] != sid
    assert call(srv, "POST", f"{BASE}/{sid}/duplicate", {"name": " "})[0] == 400
    assert call(srv, "POST", f"{BASE}/nope/duplicate", {"name": "x"})[0] == 404
    assert call(srv, "GET", f"{BASE}/{sid}")[1]["name"] == "Fresh name"


# --- the set's own sheet layouts (dsgnrfhmn, 2026-10-24) ------------------------------

def _connection_overview(db):
    with db.read() as c:
        rec = next(r for r in store.list_sets(c, "portfolio") if r.name == "Harbour overview")
    return rec.id, rec.content[0]["id"]


def test_a_layout_plan_is_stored_validated_undone_and_kept_by_an_entry_edit(db, source):
    sid, eid = _connection_overview(db)
    with db.tx() as c:
        rec = portfolio_sets.set_layout(c, sid, eid, ["side", "pair"])
    assert rec.content[0]["layout_plan"] == ["side", "pair"]
    for bad in ("side", ["pair"], ["side", "band"], ["side", 3]):
        with pytest.raises(ValueError), db.tx() as c:
            portfolio_sets.set_layout(c, sid, eid, bad)
    with db.tx() as c:
        e = store.get(c, "portfolio", sid).content[0]
        amount = "middle" if e["text_amount"] != "middle" else "short"
        rec = portfolio_sets.edit_entry(c, source, sid, eid, {"project_id": e["project_id"], "works": e["works"],
                                                              "exhibitions": e["exhibitions"], "text_amount": amount})
    assert rec.content[0]["layout_plan"] == ["side", "pair"] and rec.content[0]["text_amount"] == amount
    with db.tx() as c:
        store.undo_last(c, "portfolio", sid)               # the entry edit
        rec = store.undo_last(c, "portfolio", sid)         # the layout
    assert "layout_plan" not in rec.content[0]
    with db.tx() as c:
        portfolio_sets.set_layout(c, sid, eid, ["side"])
        rec = portfolio_sets.set_layout(c, sid, eid, [])
    assert "layout_plan" not in rec.content[0]                  # [] is the automatic layout again


def test_a_copy_of_the_set_does_not_carry_its_layouts(db):
    sid, eid = _connection_overview(db)
    with db.tx() as c:
        portfolio_sets.set_layout(c, sid, eid, ["band", "triple"])
        copy = store.duplicate(c, "portfolio", sid, "Copy", "2026-10-24T12:00:00Z")
        original = store.get(c, "portfolio", sid)
    assert original.content[0]["layout_plan"] == ["band", "triple"]
    assert all("layout_plan" not in e for e in copy.content) and copy.content[0]["works"] == original.content[0]["works"]


def test_layout_over_http(srv):
    items = call(srv, "GET", BASE)[1]["items"]
    sid = next(i["id"] for i in items if i["name"] == "Harbour overview")
    eid = call(srv, "GET", f"{BASE}/{sid}")[1]["entries"][0]["id"]
    status, body = call(srv, "PUT", f"{BASE}/{sid}/entries/{eid}/layout", {"plan": ["side", "triple"]})
    assert status == 200 and body["entries"][0]["layout_plan"] == ["side", "triple"] and body["can_undo"]
    pages = call(srv, "GET", f"{BASE}/{sid}/preview")[1]["pages"]
    assert [p["layout"] for p in pages if p.get("plan_index") is not None][:2] == ["side", "triple"]
    assert call(srv, "PUT", f"{BASE}/{sid}/entries/{eid}/layout", {"plan": ["pair"]})[0] == 400
    assert call(srv, "PUT", f"{BASE}/{sid}/entries/nope/layout", {"plan": []})[0] == 404


# --- the set's own texts (dsgnrfhmn, 2026-10-24) ---------------------------------------

def test_texts_are_the_sets_own_and_reset_to_the_project(db, source):
    sid, eid = _connection_overview(db)
    with db.read() as c:
        before = {r.id: r.content for r in store.list_sets(c, "portfolio")}
        image_id = store.get(c, "portfolio", sid).content[0]["works"][0]
    with db.tx() as c:
        rec = portfolio_sets.set_texts(c, sid, eid, {"title": " Mine ", "text": "My text.", "captions": {image_id: "Cap"}})
    e = rec.content[0]
    assert (e["title_override"], e["text_override"], e["caption_overrides"]) == ("Mine", "My text.", {image_id: "Cap"})
    with db.read() as c:
        after = {r.id: r.content for r in store.list_sets(c, "portfolio")}
    assert all(after[k] == v for k, v in before.items() if k != sid)          # no other set changed
    assert portfolio_sets.preview(rec, source)["pages"][0]["title"] == "Mine"
    with db.tx() as c:
        rec = portfolio_sets.set_texts(c, sid, eid, {"title": "", "captions": {image_id: None}})
    assert "title_override" not in rec.content[0] and "caption_overrides" not in rec.content[0]
    assert rec.content[0]["text_override"] == "My text."
    for bad in ({}, {"name": "x"}, {"title": 3}, {"captions": {"not-an-image": "x"}}, {"text": "x" * 20_001}):
        with pytest.raises(ValueError), db.tx() as c:
            portfolio_sets.set_texts(c, sid, eid, bad)
    with db.tx() as c:
        store.undo_last(c, "portfolio", sid)
        rec = store.undo_last(c, "portfolio", sid)
    assert "title_override" not in rec.content[0] and "text_override" not in rec.content[0]


def test_texts_over_http(srv):
    items = call(srv, "GET", BASE)[1]["items"]
    sid = next(i["id"] for i in items if i["name"] == "Harbour overview")
    eid = call(srv, "GET", f"{BASE}/{sid}")[1]["entries"][0]["id"]
    status, body = call(srv, "PUT", f"{BASE}/{sid}/entries/{eid}/texts", {"subtitle": "2019-2024"})
    assert status == 200 and body["entries"][0]["subtitle_override"] == "2019-2024"
    assert call(srv, "GET", f"{BASE}/{sid}/preview")[1]["pages"][0]["subtitle"] == "2019-2024"
    assert call(srv, "PUT", f"{BASE}/{sid}/entries/{eid}/texts", {"color": "red"})[0] == 400


def test_an_exhibition_plan_is_its_own_and_not_copied(db):
    sid, eid = _connection_overview(db)
    with db.tx() as c:
        rec = portfolio_sets.set_layout(c, sid, eid, ["pair", "full"], "exhibitions")
        assert rec.content[0]["exhibition_plan"] == ["pair", "full"] and "layout_plan" not in rec.content[0]
        rec = portfolio_sets.set_layout(c, sid, eid, ["side"])                  # works plan, separate
        assert rec.content[0]["exhibition_plan"] == ["pair", "full"] and rec.content[0]["layout_plan"] == ["side"]
        copy = store.duplicate(c, "portfolio", sid, "Copy", "2026-10-24T12:00:00Z")
    assert all("exhibition_plan" not in e and "layout_plan" not in e for e in copy.content)
    for bad in ((["side"], "exhibitions"), (["pair"], "both")):
        with pytest.raises(ValueError), db.tx() as c:
            portfolio_sets.set_layout(c, sid, eid, *bad)


# --- the set's own cover texts (dsgnrfhmn, 2026-10-24) ---------------------------------

COVER = {"layout": "cover", "cover": "fp", "photo": True, "texts": {
    "title": "PORTFOLIO", "name": "Artist", "details": ["one", "two"], "heading": "BIO",
    "paragraphs": ["First.", "Second."], "contacts": ["a@b.c"]}}


def test_cover_texts_are_the_sets_own_undone_and_never_a_project_entry(db, source):
    sid, eid = _connection_overview(db)
    with db.read() as c:
        before = {r.id: r.content for r in store.list_sets(c, "portfolio")}
    with db.tx() as c:
        rec = portfolio_sets.set_cover_texts(c, sid, {"name": " Mine ", "details": "x\n\ny", "paragraphs": "P1\nstill\n\nP2",
                                                       "contacts": "c1\nc2"})
    assert portfolio_sets.cover_texts(rec.content) == {"name": "Mine", "details": ["x", "y"], "paragraphs": ["P1 still", "P2"],
                                                       "contacts": ["c1", "c2"]}
    assert portfolio_sets.entries(rec.content) == before[sid]                      # the projects are untouched
    with db.read() as c:
        after = {r.id: r.content for r in store.list_sets(c, "portfolio")}
    assert all(after[k] == v for k, v in before.items() if k != sid)
    texts = portfolio_sets.preview(rec, source, cover=COVER)["pages"][0]["texts"]
    assert (texts["name"], texts["paragraphs"], texts["title"]) == ("Mine", ["P1 still", "P2"], "PORTFOLIO")
    assert COVER["texts"]["name"] == "Artist"                                       # the cover itself is not changed
    shown = portfolio_sets.describe(rec, source, has_cover=True)
    ids = [e["id"] for e in before[sid]]
    assert [e["id"] for e in shown["entries"]] == ids and shown["pages"] == len(portfolio_sets.preview(rec, source)["pages"]) + 1
    with db.tx() as c:
        rec = portfolio_sets.reorder(c, sid, ids[::-1])                              # the cover is not one of the entries
        assert [e["id"] for e in portfolio_sets.entries(rec.content)] == ids[::-1]
        rec = portfolio_sets.reorder(c, sid, ids)
        assert portfolio_sets.cover_texts(rec.content)["name"] == "Mine"
        for call_ in (lambda: portfolio_sets.delete_entry(c, sid, "cover"),
                      lambda: portfolio_sets.set_texts(c, sid, "cover", {"title": "x"})):
            with pytest.raises(NotFound):
                call_()
        copy = store.duplicate(c, "portfolio", sid, "Cover copy", "2026-10-24T12:00:00Z")
        assert portfolio_sets.cover_texts(copy.content)["name"] == "Mine"            # carried like the other text edits
        rec = portfolio_sets.apply_ai_edit(c, sid, rec.content, portfolio_sets.entries(rec.content))
        assert portfolio_sets.cover_texts(rec.content)["name"] == "Mine"            # Edit with AI keeps it
        rec = portfolio_sets.set_cover_texts(c, sid, {"name": "", "details": None, "paragraphs": "  ", "contacts": ""})
    assert portfolio_sets.cover_texts(rec.content) == {} and rec.content == before[sid]   # all back: no cover element
    for bad in ({}, {"color": "x"}, {"name": 3}, {"title": "x" * 101}):
        with pytest.raises(ValueError), db.tx() as c:
            portfolio_sets.set_cover_texts(c, sid, bad)
    with db.tx() as c:
        rec = store.undo_last(c, "portfolio", sid)
    assert portfolio_sets.cover_texts(rec.content)["name"] == "Mine"


def test_cover_texts_over_http(srv):
    items = call(srv, "GET", BASE)[1]["items"]
    one = next(i for i in items if i["name"] == "Harbour overview")
    status, body = call(srv, "PUT", f"{BASE}/{one['id']}/cover", {"heading": "About"})
    assert status == 200 and [e["id"] for e in body["entries"]] == [e["id"] for e in call(srv, "GET", f"{BASE}/{one['id']}")[1]["entries"]]
    assert all("kind" not in e for e in body["entries"])
    again = next(i for i in call(srv, "GET", BASE)[1]["items"] if i["id"] == one["id"])
    assert again["entry_count"] == one["entry_count"]
    assert call(srv, "PUT", f"{BASE}/{one['id']}/cover", {"color": "red"})[0] == 400


# --- deleting one page of an entry (dsgnrfhmn, 2026-10-24) -----------------------------

def _pages(rec, source, entry_id):
    return [s for s in portfolio_sets.preview(rec, source)["pages"] if s["entry_id"] == entry_id]


def test_deleting_a_works_page_takes_its_pictures_keeps_the_other_layouts_and_undoes(db, source, project):
    with db.tx() as c:
        sid = store.create(c, "portfolio", "2026-10-24T12:00:00Z", name="Pages").id
        rec = portfolio_sets.add_entry(c, source, sid, entry(project, works=range(len(project[1]))))
        eid = rec.content[0]["id"]
        rec = portfolio_sets.set_layout(c, sid, eid, ["figure-side"] + ["full"] * (len(project[1]) - 1))
    before = _pages(rec, source, eid)
    works = [s for s in before if s.get("plan_index") is not None]
    assert len(works) >= 3
    target = works[1]
    gone = {i["id"] for i in target["images"]}
    with db.tx() as c:
        rec2 = portfolio_sets.delete_sheet(c, source, sid, eid, "works", 1)
    e = next(x for x in rec2.content if x["id"] == eid)
    assert gone and not gone & set(e["works"]) and len(e["works"]) == len(rec.content[0]["works"]) - len(gone)
    after = [s for s in _pages(rec2, source, eid) if s.get("plan_index") is not None]
    assert [s["layout"] for s in after] == [s["layout"] for s in works if s is not target]   # the rest look the same
    assert [s["images"] for s in after] == [s["images"] for s in works if s is not target]
    with db.tx() as c:
        undone = store.undo_last(c, "portfolio", sid)
    assert undone.content == rec.content


def test_deleting_the_opening_takes_its_text_and_an_exhibition_page_its_view(db, source):
    sid, eid = _connection_overview(db)
    with db.read() as c:
        rec = store.get(c, "portfolio", sid)
    opening = _pages(rec, source, eid)[0]
    assert opening["plan_index"] == 0 and opening["paragraphs"]
    with db.tx() as c:
        rec2 = portfolio_sets.delete_sheet(c, source, sid, eid, "works", 0)
    e = next(x for x in rec2.content if x["id"] == eid)
    assert e["text_override"] == "" and not {i["id"] for i in opening["images"]} & set(e["works"])
    left = _pages(rec2, source, eid)
    assert not e["works"] and all(s.get("plan_index") is None for s in left)      # its 4 works were all on it
    assert all(not s["paragraphs"] for s in left) and left[0].get("exhibition_index") == 0
    views = [s for s in left if s.get("exhibition_index") is not None]
    with db.tx() as c:
        rec3 = portfolio_sets.delete_sheet(c, source, sid, eid, "exhibitions", 0)
    e3 = next(x for x in rec3.content if x["id"] == eid)
    assert views[0]["images"][0]["id"] not in e3["exhibitions"]
    for bad_part, bad_index in (("both", 0), ("works", -1), ("works", "1"), ("works", True)):
        with pytest.raises(ValueError), db.tx() as c:
            portfolio_sets.delete_sheet(c, source, sid, eid, bad_part, bad_index)
    with pytest.raises(NotFound), db.tx() as c:
        portfolio_sets.delete_sheet(c, source, sid, eid, "works", 99)


def test_deleting_every_page_of_an_entry_takes_the_entry_out_of_the_set(db, source):
    sid, eid = _connection_overview(db)
    with db.tx() as c:
        for _ in range(40):
            rec = store.get(c, "portfolio", sid)
            pages = _pages(rec, source, eid)
            page = next((s for s in pages if s.get("plan_index") is not None or s.get("exhibition_index") is not None), None)
            if page is None:
                break
            part, key = ("works", "plan_index") if page.get("plan_index") is not None else ("exhibitions", "exhibition_index")
            rec = portfolio_sets.delete_sheet(c, source, sid, eid, part, page[key])
    assert eid not in [e["id"] for e in portfolio_sets.entries(rec.content)]


def test_delete_a_page_over_http(srv):
    items = call(srv, "GET", BASE)[1]["items"]
    sid = next(i["id"] for i in items if i["name"] == "Harbour overview")
    one = call(srv, "GET", f"{BASE}/{sid}")[1]
    eid, pages = one["entries"][0]["id"], one["pages"]
    status, body = call(srv, "DELETE", f"{BASE}/{sid}/entries/{eid}/sheets/exhibitions/1")
    assert status == 200 and body["pages"] == pages - 1 and body["can_undo"]
    assert call(srv, "DELETE", f"{BASE}/{sid}/entries/{eid}/sheets/works/x")[0] == 400
    assert call(srv, "DELETE", f"{BASE}/{sid}/entries/{eid}/sheets/works/99")[0] == 404


def test_deleting_the_opening_keeps_the_text_that_runs_on_after_it(db, source, project):
    long = "\n\n".join(f"Paragraph {n} " + "word " * 60 for n in range(8))
    with db.tx() as c:
        sid = store.create(c, "portfolio", "2026-10-24T12:00:00Z", name="Long text").id
        rec = portfolio_sets.add_entry(c, source, sid, entry(project, works=(0, 1)))
        eid = rec.content[0]["id"]
        rec = portfolio_sets.set_texts(c, sid, eid, {"text": long})
        shown = _pages(rec, source, eid)[0]["paragraphs"]
        rec = portfolio_sets.delete_sheet(c, source, sid, eid, "works", 0)
    left = next(x for x in rec.content if x["id"] == eid)["text_override"]
    assert shown and left and not any(p in left for p in shown)
    assert left.startswith(f"Paragraph {len(shown)} ")
    assert _pages(rec, source, eid)[0]["paragraphs"][0].startswith(f"Paragraph {len(shown)} ")
