"""Tracker: Mark Applied, status, the Tracker's own archive, replies and Refresh.
Model layer and HTTP layer, against the real seed and the fixture mailbox."""

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from core.config import Config
from core.db import Database
from core.seed import seed_opportunities, seed_updates
from models import opportunity, update
from sources.fixture_mail_updates import FixtureMailUpdates
from sources.mail_updates import IncomingUpdate, MailError
from web import server as s

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SEED = json.loads((FIXTURES / "opportunities.json").read_text("utf-8"))
MAIL = json.loads((FIXTURES / "updates.json").read_text("utf-8"))
JSON_H = {"Content-Type": "application/json"}
NOW = "2026-09-20T12:00:00Z"


# Names are not unique in the fixtures (two "Fjord Arts Mobility Grant"), so ids.
WEXFORD = "opp-fixture-0031"   # grant, applied, 2 seeded replies
BASQUE = "opp-fixture-0038"    # grant, approved, 3 seeded replies
DRESDEN = "opp-fixture-0043"   # grant, declined, adversarial reply
FJORD = "opp-fixture-0048"     # open call, applied, reply arrives on Refresh
GDANSK = "opp-fixture-0053"    # open call, archived in the Tracker, one seeded reply, one on Refresh
NOT_APPLIED = next(o["id"] for o in SEED if o["stage"] == "analysed" and not o.get("application") and not o.get("archived"))
REVIEW = next(o["id"] for o in SEED if o["stage"] == "for_review")
FILTERED = next(o["id"] for o in SEED if o["stage"] == "filtered")
ARCHIVED_ROW = next(o["id"] for o in SEED if o.get("archived") and not o.get("application"))


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "arta.sqlite3")
    d.migrate()
    seed_opportunities(d, FIXTURES)
    seed_updates(d, FIXTURES)
    return d


@pytest.fixture
def mail():
    return FixtureMailUpdates(FIXTURES / "updates.json")


@pytest.fixture
def srv(db, tmp_path, mail):
    cfg = Config(updates="fixtures", projects="fixtures", cv="fixtures", llm="mock", port=0, data_dir=tmp_path / "data")
    server = s.make_server(cfg, db, port=0, mail=mail)
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


def one(db, i):
    return opportunity.get(db, i)


def tracker(srv, archived):
    return call(srv, "GET", f"/api/opportunities?applied=true&tracker_archived={str(archived).lower()}")[1]["items"]


# --- seeded state -------------------------------------------------------------------

def test_seed_has_every_tracker_state(db):
    assert {o.id for o in opportunity.list_all(db, applied=True)} == {WEXFORD, BASQUE, DRESDEN, FJORD, GDANSK}
    assert {i: one(db, i).application.status for i in (WEXFORD, BASQUE, DRESDEN)} == {
        WEXFORD: "applied", BASQUE: "approved", DRESDEN: "declined"}
    assert one(db, GDANSK).application.archived and not one(db, GDANSK).archived
    assert one(db, WEXFORD).decision_date and one(db, FJORD).residency_start   # what the Dates column shows


# --- Mark Applied -------------------------------------------------------------------

def test_mark_applied_adds_the_tracker_row_and_keeps_the_analysed_row(srv, db):
    status, body = call(srv, "POST", "/api/opportunities/apply", {"ids": [NOT_APPLIED]})
    assert status == 200
    rec = body["items"][0]
    assert rec["application"]["status"] == "applied" and rec["application"]["archived"] is False
    assert rec["updates"] == {"total": 0, "unread": 0}
    assert rec["stage"] == "analysed" and rec["flags"]["archived"] is False
    in_tracker = [i["id"] for i in tracker(srv, False)]
    in_analysed = [i["id"] for i in call(srv, "GET", "/api/opportunities?stage=analysed&archived=false")[1]["items"]]
    assert NOT_APPLIED in in_tracker and NOT_APPLIED in in_analysed
    assert in_tracker[0] == NOT_APPLIED                # newest applied first


def test_prepared_is_not_needed_and_never_reaches_the_tracker(srv, db):
    prepared = next(o for o in SEED if o["stage"] == "analysed" and o.get("prepared") and not o.get("application"))
    status, body = call(srv, "POST", "/api/opportunities/apply", {"ids": [prepared["id"]]})
    assert status == 200 and body["items"][0]["application"]["status"] == "applied"


@pytest.mark.parametrize("target,why", [
    (REVIEW, "for review"), (FILTERED, "filtered"), (ARCHIVED_ROW, "archived"), (WEXFORD, "already applied"),
])
def test_mark_applied_refuses_what_is_not_an_analysed_open_row(srv, db, target, why):
    status, body = call(srv, "POST", "/api/opportunities/apply", {"ids": [target]})
    assert status == 409, why
    assert one(db, target).application is None or target == WEXFORD


def test_mark_applied_is_one_operation(srv, db):
    status, _ = call(srv, "POST", "/api/opportunities/apply", {"ids": [NOT_APPLIED, REVIEW]})
    assert status == 409 and one(db, NOT_APPLIED).application is None
    assert call(srv, "POST", "/api/opportunities/apply", {"ids": [NOT_APPLIED, "nope"]})[0] == 404
    assert one(db, NOT_APPLIED).application is None
    assert call(srv, "POST", "/api/opportunities/apply", {"ids": []})[0] == 400
    assert call(srv, "POST", "/api/opportunities/apply", {"ids": [NOT_APPLIED, NOT_APPLIED]})[0] == 400
    assert call(srv, "POST", "/api/opportunities/apply", [])[0] == 400


# --- status ---------------------------------------------------------------------------

@pytest.mark.parametrize("status", ["applied", "approved", "declined"])
def test_status_persists(srv, db, status):
    code, body = call(srv, "PUT", f"/api/opportunities/{WEXFORD}/application", {"status": status})
    assert code == 200 and body["application"]["status"] == status
    assert one(db, WEXFORD).application.status == status


@pytest.mark.parametrize("bad", ["prepared", "", None, 3, "Applied", "archived", "pending"])
def test_only_the_three_application_statuses_are_accepted(srv, db, bad):
    assert call(srv, "PUT", f"/api/opportunities/{WEXFORD}/application", {"status": bad})[0] == 400
    assert one(db, WEXFORD).application.status == "applied"


def test_status_on_an_analysed_record_creates_its_application(srv, db):
    # Choosing a status in For You is a Mark Applied that may start at any status.
    assert one(db, NOT_APPLIED).application is None
    code, body = call(srv, "PUT", f"/api/opportunities/{NOT_APPLIED}/application", {"status": "declined"})
    assert code == 200 and body["application"]["status"] == "declined"
    assert body["application"]["applied_at"] and body["application"]["archived"] is False
    assert body["stage"] == "analysed"  # it stays in Analysed, as after Mark Applied
    assert one(db, NOT_APPLIED).application.status == "declined"
    assert NOT_APPLIED in [o["id"] for o in tracker(srv, False)]


def test_status_needs_an_analysed_record_that_is_not_archived(srv, db):
    for opp in (REVIEW, FILTERED, ARCHIVED_ROW):
        assert call(srv, "PUT", f"/api/opportunities/{opp}/application", {"status": "approved"})[0] == 409
        assert one(db, opp).application is None
    assert call(srv, "PUT", "/api/opportunities/nope/application", {"status": "approved"})[0] == 404


def test_status_goes_back_without_deleting_anything(srv, db):
    # Applied -> Analysed takes the application off; its replies stay stored.
    replies = len(call(srv, "GET", f"/api/opportunities/{WEXFORD}/updates")[1]["items"])
    code, body = call(srv, "PUT", f"/api/opportunities/{WEXFORD}/application", {"status": "analysed"})
    assert code == 200 and body["stage"] == "analysed" and body["application"] is None
    assert body["fields"]["summary"]                                          # the analysis stays
    # Applied again: the replies are there again.
    assert call(srv, "PUT", f"/api/opportunities/{WEXFORD}/application", {"status": "applied"})[0] == 200
    assert len(call(srv, "GET", f"/api/opportunities/{WEXFORD}/updates")[1]["items"]) == replies
    # -> For review: the verdict goes with its origin (a For review record has none), the
    # dates it read stay; Analysed is then refused until it is analysed again.
    before = call(srv, "GET", f"/api/opportunities/{WEXFORD}")[1]["fields"]
    code, body = call(srv, "PUT", f"/api/opportunities/{WEXFORD}/application", {"status": "for_review"})
    assert code == 200 and body["stage"] == "for_review" and body["application"] is None
    assert not body["fields"].get("summary") and not body["fields"].get("analysis_origin")
    assert body["fields"].get("deadline") == before.get("deadline")
    assert call(srv, "PUT", f"/api/opportunities/{WEXFORD}/application", {"status": "analysed"})[0] == 409


def test_status_back_has_its_conditions(srv, db):
    # Never analysed: Analysed is refused. Filtered: For review is the promote.
    assert call(srv, "PUT", f"/api/opportunities/{REVIEW}/application", {"status": "analysed"})[0] == 409
    assert call(srv, "PUT", f"/api/opportunities/{FILTERED}/application", {"status": "analysed"})[0] == 409
    code, body = call(srv, "PUT", f"/api/opportunities/{FILTERED}/application", {"status": "for_review"})
    assert code == 200 and body["stage"] == "for_review" and not body["fields"].get("filter_reason")
    assert call(srv, "PUT", f"/api/opportunities/{ARCHIVED_ROW}/application", {"status": "for_review"})[0] == 409


def test_status_of_an_archived_record_that_has_an_application_can_change(srv, db):
    call(srv, "POST", "/api/opportunities/archive", {"ids": [BASQUE]})
    assert call(srv, "PUT", f"/api/opportunities/{BASQUE}/application", {"status": "declined"})[0] == 200
    assert one(db, BASQUE).application.status == "declined" and one(db, BASQUE).archived


def test_status_is_kept_by_the_for_you_archive(srv, db):
    call(srv, "POST", "/api/opportunities/archive", {"ids": [BASQUE]})
    assert one(db, BASQUE).application.status == "approved"


# --- the For You archive leaves the application alone ------------------------------------------------------

def test_for_you_archive_touches_only_for_yous_flag(srv, db):
    call(srv, "POST", "/api/opportunities/archive", {"ids": [WEXFORD]})
    assert one(db, WEXFORD).archived and not one(db, WEXFORD).application.archived
    assert WEXFORD in [i["id"] for i in tracker(srv, False)]          # still in the Tracker's Active tab


def test_the_schema_itself_refuses_a_tracker_archive_without_an_application(db):
    with pytest.raises(Exception):
        with db.tx() as c:
            c.execute("UPDATE opportunities SET application_archived = 1, application_archived_at = ? WHERE id = ?",
                      (NOW, NOT_APPLIED))


# --- listing -----------------------------------------------------------------------------

def test_the_tracker_tabs_are_queries_and_ordered(srv):
    active, archived = tracker(srv, False), tracker(srv, True)
    assert {i["id"] for i in active} == {WEXFORD, BASQUE, DRESDEN, FJORD}
    assert [i["id"] for i in archived] == [GDANSK]
    stamps = [i["application"]["applied_at"] for i in active]
    assert stamps == sorted(stamps, reverse=True)
    assert all(i["updates"] is not None for i in active + archived)


def test_tracker_archived_needs_applied(srv):
    assert call(srv, "GET", "/api/opportunities?tracker_archived=true")[0] == 400
    assert call(srv, "GET", "/api/opportunities?applied=false&tracker_archived=false")[0] == 400
    assert call(srv, "GET", "/api/opportunities?applied=true&tracker_archived=maybe")[0] == 400


def test_rows_that_are_not_applied_carry_no_updates(srv):
    rows = call(srv, "GET", "/api/opportunities?stage=for_review")[1]["items"]
    assert rows and all(r["updates"] is None for r in rows)


def test_one_note_for_both_views(srv):
    assert call(srv, "PATCH", f"/api/opportunities/{FJORD}", {"notes": "Sent Friday"})[0] == 200
    tab = next(i for i in tracker(srv, False) if i["id"] == FJORD)
    ana = next(i for i in call(srv, "GET", "/api/opportunities?stage=analysed&archived=false")[1]["items"] if i["id"] == FJORD)
    assert tab["fields"]["notes"] == ana["fields"]["notes"] == "Sent Friday"


# --- replies -------------------------------------------------------------------------------

def counts(srv):
    return {i["id"]: i["updates"] for i in tracker(srv, False) + tracker(srv, True)}


def test_seeded_replies_are_counted_and_unread(srv):
    c = counts(srv)
    assert c[WEXFORD] == {"total": 2, "unread": 2} and c[BASQUE] == {"total": 3, "unread": 3}
    assert c[DRESDEN] == {"total": 1, "unread": 1} and c[FJORD] == {"total": 0, "unread": 0}
    assert c[GDANSK] == {"total": 1, "unread": 1}
    assert call(srv, "GET", "/api/health")[1]["unread"] == 7


def test_replies_newest_first_with_sender_date_and_body(srv):
    items = call(srv, "GET", f"/api/opportunities/{BASQUE}/updates")[1]["items"]
    assert [i["subject"] for i in items] == ["Congratulations", "Shortlist", "Thank you for applying"]
    assert all(set(i) == {"id", "sender", "received_at", "subject", "body", "read"} and i["read"] is False for i in items)
    assert call(srv, "GET", f"/api/opportunities/{NOT_APPLIED}/updates")[1] == {"items": []}
    assert call(srv, "GET", "/api/opportunities/nope/updates")[0] == 404


def test_a_reply_is_inert_text(srv, db):
    """Markup is stripped, and an instruction in a body changes nothing."""
    (item,) = call(srv, "GET", f"/api/opportunities/{DRESDEN}/updates")[1]["items"]
    assert "<" not in item["body"] and "alert" not in item["body"]
    assert "Unfortunately" in item["body"] and "Ignore previous instructions" in item["body"]
    assert one(db, DRESDEN).application.status == "declined"       # the "approve it" line did nothing


def test_opening_the_popup_reads_the_row_and_keeps_the_total(srv):
    status, body = call(srv, "POST", f"/api/opportunities/{BASQUE}/updates/read", {})
    assert status == 200
    assert body["opportunity"]["updates"] == {"total": 3, "unread": 0} and body["unread"] == 4
    assert all(i["read"] for i in call(srv, "GET", f"/api/opportunities/{BASQUE}/updates")[1]["items"])
    assert call(srv, "GET", "/api/health")[1]["unread"] == 4
    assert call(srv, "POST", f"/api/opportunities/{BASQUE}/updates/read", {})[0] == 200        # idempotent
    assert call(srv, "POST", "/api/opportunities/nope/updates/read", {})[0] == 404


def test_refresh_stores_what_is_new_once_and_reaches_archived_rows(srv):
    status, body = call(srv, "POST", "/api/tracker/refresh", {})
    assert status == 200 and body == {"matched": 5, "new": 2, "unread": 9}
    c = counts(srv)
    assert c[FJORD] == {"total": 1, "unread": 1}
    assert c[GDANSK] == {"total": 2, "unread": 2}                  # an archived row is refreshed too
    again = call(srv, "POST", "/api/tracker/refresh", {})[1]
    assert again == {"matched": 5, "new": 0, "unread": 9}          # the seen-set: nothing is stored twice
    assert counts(srv) == c


def test_refresh_keeps_read_state(srv):
    call(srv, "POST", f"/api/opportunities/{WEXFORD}/updates/read", {})
    call(srv, "POST", "/api/tracker/refresh", {})
    assert counts(srv)[WEXFORD] == {"total": 2, "unread": 0}


def test_a_reply_for_a_row_that_is_not_applied_waits_for_it(srv, db):
    assert call(srv, "POST", "/api/tracker/refresh", {})[0] == 200
    assert call(srv, "GET", f"/api/opportunities/{NOT_APPLIED}/updates")[1]["items"] == []
    call(srv, "POST", "/api/opportunities/apply", {"ids": [NOT_APPLIED]})
    body = call(srv, "POST", "/api/tracker/refresh", {})[1]
    assert body["new"] == 1
    assert len(call(srv, "GET", f"/api/opportunities/{NOT_APPLIED}/updates")[1]["items"]) == 1


def test_refresh_needs_a_json_body(srv):
    assert call(srv, "POST", "/api/tracker/refresh", None, headers={})[0] == 415


def test_refresh_without_a_mail_source_is_503_not_an_empty_success(db, tmp_path):
    cfg = Config(llm="mock", port=0, data_dir=tmp_path / "d2")      # build_mail gives None
    server = s.make_server(cfg, db, port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        assert call(server, "POST", "/api/tracker/refresh", {})[0] == 503
    finally:
        server.shutdown()
        server.server_close()


class BrokenMail:
    def fetch(self, applied):
        raise MailError("The mailbox could not be read.")


def test_a_broken_mailbox_is_502_and_stores_nothing(db, tmp_path):
    cfg = Config(updates="fixtures", projects="fixtures", cv="fixtures", llm="mock", port=0, data_dir=tmp_path / "d3")
    server = s.make_server(cfg, db, port=0, mail=BrokenMail())
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        before = counts_of(db)
        status, body = call(server, "POST", "/api/tracker/refresh", {})
        assert status == 502 and "mailbox" in body["error"]["message"]
        assert counts_of(db) == before
    finally:
        server.shutdown()
        server.server_close()


def counts_of(db):
    with db.read() as c:
        return c.execute("SELECT COUNT(*) FROM updates").fetchone()[0]


# --- the mail layer ---------------------------------------------------------------------------

def test_store_is_idempotent_and_sanitizes(db):
    msg = IncomingUpdate("m-1", WEXFORD, "<i>a@b.example</i>", NOW, "<b>Hi</b>", "x<script>y</script>z & more")
    with db.tx() as c:
        assert update.store(c, [msg]) == 1
        assert update.store(c, [msg]) == 0
        row = c.execute("SELECT sender, subject, body FROM updates WHERE message_id = 'm-1'").fetchone()
    assert tuple(row) == ("a@b.example", "Hi", "xz & more")


def test_an_over_long_body_is_refused_not_stored_half(db):
    with pytest.raises(ValueError):
        with db.tx() as c:
            update.store(c, [IncomingUpdate("m-2", WEXFORD, "a@b.example", NOW, "s", "x" * (update.BODY_MAX + 1))])
    with db.read() as c:
        assert c.execute("SELECT COUNT(*) FROM updates WHERE message_id = 'm-2'").fetchone()[0] == 0


def test_deleting_an_opportunity_removes_its_replies(db):
    with db.tx() as c:
        c.execute("DELETE FROM opportunities WHERE id = ?", (BASQUE,))
    with db.read() as c:
        assert c.execute("SELECT COUNT(*) FROM updates WHERE opportunity_id = ?", (BASQUE,)).fetchone()[0] == 0


def test_fixture_mail_only_returns_applied_rows(mail, db):
    applied = opportunity.list_all(db, applied=True)
    got = mail.fetch(applied)
    assert {u.opportunity_id for u in got} == {WEXFORD, BASQUE, DRESDEN, FJORD, GDANSK}
    assert len(got) == 9 and all(u.opportunity_id in {a.id for a in applied} for u in got)


def test_seed_updates_seeds_the_marked_messages_only_and_once(db):
    assert counts_of(db) == sum(1 for m in MAIL if m["seed"])
    assert seed_updates(db, FIXTURES) == 0


def test_seed_updates_raises_on_an_unknown_opportunity(tmp_path):
    fx = tmp_path / "fx"
    fx.mkdir()
    bad = [{**MAIL[0], "opportunity_id": "opp-nope"}]
    (fx / "updates.json").write_text(json.dumps(bad))
    d = Database(tmp_path / "a.sqlite3")
    d.migrate()
    seed_opportunities(d, FIXTURES)
    with pytest.raises(ValueError, match="unknown opportunity"):
        seed_updates(d, fx)


@pytest.mark.parametrize("mutate,message", [
    (lambda m: m.append(dict(m[0])), "twice"),
    (lambda m: m[0].pop("body"), "fields"),
    (lambda m: m[0].update(extra=1), "fields"),
])
def test_a_bad_mail_fixture_raises(tmp_path, mutate, message):
    data = json.loads((FIXTURES / "updates.json").read_text("utf-8"))
    mutate(data)
    path = tmp_path / "updates.json"
    path.write_text(json.dumps(data))
    with pytest.raises(MailError, match=message):
        FixtureMailUpdates(path)


def test_upgrading_a_database_from_before_the_tracker(tmp_path, monkeypatch):
    """A database made at schema 4 (the deployed one) keeps every record, starts with
    no replies, and takes the replies seed after the upgrade."""
    from core import db as core_db

    d = Database(tmp_path / "old.sqlite3")
    monkeypatch.setattr(core_db, "MIGRATIONS", core_db.MIGRATIONS[:4])
    assert d.migrate() == 4

    def insert_v4(conn, o):
        """What insert() wrote before the body column (schema 8) existed."""
        conn.execute(
            "INSERT INTO opportunities (id, stage, prepared_cv_set_id, application_status, applied_at,"
            " archived, archived_at, saved, name, type, source, link, description, summary, eligible,"
            " relevance, deadline, residency_start, residency_end, decision_date, notes, analysis_origin,"
            " created_at, application_archived, application_archived_at, filter_reason)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (o.id, o.stage, o.prepared, o.application.status if o.application else None,
             o.application.applied_at if o.application else None, int(o.archived), o.archived_at,
             int(o.saved), o.name, o.type, o.source, o.link, o.description, o.summary, o.eligible,
             o.relevance, o.deadline, o.residency_start, o.residency_end, o.decision_date, o.notes,
             o.analysis_origin, o.created_at, int(o.application.archived) if o.application else 0,
             o.application.archived_at if o.application else None, o.filter_reason))

    monkeypatch.setattr(opportunity, "insert", insert_v4)
    seed_opportunities(d, FIXTURES)
    monkeypatch.undo()
    assert d.migrate() == core_db.LATEST_VERSION
    with d.read() as c:
        assert c.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0] == len(SEED)
        assert c.execute("SELECT COUNT(*) FROM updates").fetchone()[0] == 0
    assert seed_updates(d, FIXTURES) > 0
