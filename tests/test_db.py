import sqlite3
import stat
import json
import threading
from pathlib import Path

import pytest

from core import db as db_mod
from core.db import Database, DbError
from core.seed import seed_opportunities
from models import opportunity

FIXTURES = Path(__file__).parent / "fixtures"
SEED = json.loads((FIXTURES / "opportunities.json").read_text("utf-8"))


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "data" / "arta.sqlite3")
    d.migrate()
    return d


def test_migrate_creates_owner_only_file_and_dir(db):
    assert stat.S_IMODE(db.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(db.path.parent.stat().st_mode) == 0o700
    assert db.version() == db_mod.LATEST_VERSION


def test_migrate_is_idempotent(db):
    assert db.migrate() == db_mod.LATEST_VERSION


def test_newer_schema_refused(db):
    with db.tx() as c:
        c.execute(f"PRAGMA user_version = {db_mod.LATEST_VERSION + 1}")
    with pytest.raises(DbError, match="newer than this code"):
        db.migrate()


def test_group_readable_file_refused(tmp_path):
    p = tmp_path / "x.sqlite3"
    p.touch()
    p.chmod(0o644)
    with pytest.raises(DbError, match="0600"):
        Database(p).migrate()


def test_failed_migration_rolls_back_including_version(db, monkeypatch):
    latest = db_mod.LATEST_VERSION + 1
    bad = db_mod.MIGRATIONS + [(latest, "bad", "CREATE TABLE t (a); CREATE TABLE t (a);")]
    monkeypatch.setattr(db_mod, "MIGRATIONS", bad)
    monkeypatch.setattr(db_mod, "LATEST_VERSION", latest)
    with pytest.raises(sqlite3.OperationalError):
        db.migrate()
    assert db.version() == latest - 1
    with db.read() as c:
        assert c.execute("SELECT 1 FROM sqlite_master WHERE name='t'").fetchone() is None


def test_tx_rolls_back_on_error(db):
    with pytest.raises(RuntimeError):
        with db.tx() as c:
            c.execute("INSERT INTO intake_seen VALUES ('m1', 'now')")
            raise RuntimeError
    with db.read() as c:
        assert c.execute("SELECT count(*) FROM intake_seen").fetchone()[0] == 0


def test_concurrent_writers_lose_no_update(db):
    with db.tx() as c:
        c.execute("CREATE TABLE counter (n INTEGER)")
        c.execute("INSERT INTO counter VALUES (0)")

    def bump():
        for _ in range(25):
            with db.tx() as c:
                n = c.execute("SELECT n FROM counter").fetchone()[0]
                c.execute("UPDATE counter SET n = ?", (n + 1,))

    threads = [threading.Thread(target=bump) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    with db.read() as c:
        assert c.execute("SELECT n FROM counter").fetchone()[0] == 100


def test_seed_populates_once_and_does_not_overwrite(db):
    assert seed_opportunities(db, FIXTURES) == len(SEED)
    with db.tx() as c:
        c.execute("UPDATE opportunities SET notes = 'mine' WHERE id = 'opp-fixture-0001'")
    assert seed_opportunities(db, FIXTURES) == 0
    assert opportunity.get(db, "opp-fixture-0001").notes == "mine"


def test_applied_row_stays_analysed_one_record_two_views(db):
    seed_opportunities(db, FIXTURES)
    want = next(r for r in SEED if r.get("application"))
    o = opportunity.get(db, want["id"])
    assert o.stage == "analysed" and o.application.status == "applied"
    assert o.to_api()["application"] == {
        "status": "applied",
        "applied_at": want["application"]["applied_at"],
        "archived": False,
        "archived_at": None,
    }
    assert o.to_api()["fields"]["analysis_origin"] == "mock"


def _row(**over):
    base = dict(id="x", stage="for_review", name="n", type="grant", source="fixtures",
                created_at="2026-01-01T00:00:00Z")
    base.update(over)
    return opportunity.Opportunity(**base)


_APPLIED = opportunity.Application("applied", "t")


@pytest.mark.parametrize("bad", [
    dict(stage="bogus"),
    dict(type="job"),
    dict(archived_at="t"),           # archived_at without archived
    dict(archived=True),             # archived without archived_at
    dict(analysis_origin="mock"),    # analysis origin on a non-analysed row
    dict(application=opportunity.Application("prepared", "t")),
    # Tracker archive flag: needs an application, and flag and timestamp agree.
    dict(application=opportunity.Application("applied", "t", archived=True)),
    dict(application=opportunity.Application("applied", "t", archived_at="t")),
    dict(filter_reason="Profile mismatch"),                      # reason on a non-filtered row
    dict(stage="analysed", filter_reason="Profile mismatch"),
])
def test_schema_rejects_impossible_records(db, bad):
    with pytest.raises(sqlite3.IntegrityError):
        with db.tx() as c:
            opportunity.insert(c, _row(**bad))


def test_tracker_archive_needs_an_application(db):
    with db.tx() as c:
        opportunity.insert(c, _row())
    with pytest.raises(sqlite3.IntegrityError):
        with db.tx() as c:
            c.execute(
                "UPDATE opportunities SET application_archived = 1, "
                "application_archived_at = 't' WHERE id = 'x'"
            )


def test_tracker_archive_and_for_you_archive_are_independent(db):
    tracker_archived = opportunity.Application("applied", "t", archived=True, archived_at="t1")
    with db.tx() as c:
        opportunity.insert(c, _row(id="a", stage="analysed", application=tracker_archived))
        opportunity.insert(c, _row(id="b", stage="analysed", application=_APPLIED,
                                   archived=True, archived_at="t2"))
    a, b = opportunity.get(db, "a"), opportunity.get(db, "b")
    # Archived in the Tracker only: still an ordinary Analysed row in For You.
    assert a.application.archived and not a.archived
    # Archived in For You only: still in the Tracker's Active tab.
    assert b.archived and not b.application.archived
    api = a.to_api()
    assert api["flags"]["archived"] is False and api["application"]["archived"] is True


def test_upgrade_from_v1_keeps_existing_rows(tmp_path, monkeypatch):
    d = Database(tmp_path / "arta.sqlite3")
    monkeypatch.setattr(db_mod, "MIGRATIONS", db_mod.MIGRATIONS[:1])
    monkeypatch.setattr(db_mod, "LATEST_VERSION", 1)
    assert d.migrate() == 1
    with d.tx() as c:
        c.execute(
            "INSERT INTO opportunities (id, stage, application_status, applied_at, name, type, "
            "source, created_at) VALUES ('old', 'analysed', 'applied', 't', 'n', 'grant', 'f', 't')"
        )
    monkeypatch.undo()
    assert d.migrate() == db_mod.LATEST_VERSION
    old = opportunity.get(d, "old")
    assert old.application == opportunity.Application("applied", "t")


def test_upgrade_from_v2_keeps_rows_and_adds_filter_reason(tmp_path, monkeypatch):
    d = Database(tmp_path / "arta.sqlite3")
    monkeypatch.setattr(db_mod, "MIGRATIONS", db_mod.MIGRATIONS[:2])
    monkeypatch.setattr(db_mod, "LATEST_VERSION", 2)
    assert d.migrate() == 2
    with d.tx() as c:
        c.execute(
            "INSERT INTO opportunities (id, stage, name, type, source, created_at) "
            "VALUES ('old', 'filtered', 'n', 'grant', 'f', 't')"
        )
    monkeypatch.undo()
    assert d.migrate() == db_mod.LATEST_VERSION >= 3
    old = opportunity.get(d, "old")
    assert old.stage == "filtered" and old.filter_reason is None


def test_schema_accepts_applied_row(db):
    with db.tx() as c:
        opportunity.insert(c, _row(application=_APPLIED))
    assert opportunity.get(db, "x").application == _APPLIED


def test_list_filters_by_stage_and_rejects_unknown(db):
    seed_opportunities(db, FIXTURES)
    assert {o.id for o in opportunity.list_all(db, "filtered")} == {r["id"] for r in SEED if r["stage"] == "filtered"}
    with pytest.raises(ValueError):
        opportunity.list_all(db, "nope")


def test_seed_covers_every_for_you_state():
    """The static page's states must all be representable and present in the seed."""
    by = lambda pred: [r for r in SEED if pred(r)]
    assert by(lambda r: r["stage"] == "for_review")
    assert by(lambda r: r["stage"] == "for_review" and r["description"] == "")       # Add Description, Analyze disabled
    assert by(lambda r: r["stage"] == "analysed" and not r.get("archived") and not r.get("prepared") and not r.get("application"))
    assert by(lambda r: r.get("prepared"))
    assert by(lambda r: r.get("application"))
    assert by(lambda r: r.get("archived"))
    assert by(lambda r: r["stage"] == "filtered" and r.get("filter_reason"))
    assert by(lambda r: r.get("saved"))
    assert by(lambda r: r.get("notes"))
    assert by(lambda r: r["stage"] == "analysed" and "deadline" not in r)            # empty date cell


def test_seed_labels_every_analysis_as_mock_and_never_invents_verdicts():
    for r in SEED:
        if r["stage"] == "analysed":
            assert r["analysis_origin"] == "mock", r["id"]
        assert "eligible" not in r and "relevance" not in r, r["id"]


def test_seed_ids_are_unique_and_links_are_non_resolvable():
    assert len({r["id"] for r in SEED}) == len(SEED)
    assert all(r["link"].startswith("https://example.invalid/") for r in SEED)


def test_mock_analyse_failure_row_is_the_intended_record():
    """The mock's canned failure (SS4.4.2) is keyed by id in tests/fixtures/llm/cases.json;
    the id must still point at that record after the seed is regenerated, and the page
    no longer carries the id itself."""
    cases = json.loads((Path(__file__).parent / "fixtures" / "llm" / "cases.json").read_text("utf-8"))
    assert "error" in cases["analysis"]["opp-fixture-0002"]
    assert "opp-fixture-0002" not in (Path(__file__).parent.parent / "www" / "for-you-rows.js").read_text("utf-8")
    rec = next(r for r in SEED if r["id"] == "opp-fixture-0002")
    assert rec["name"] == "National Arts Council Project Grant" and rec["stage"] == "for_review" and rec["description"]


def test_upgrade_from_v5_rebuilds_opportunities_keeping_rows_and_links(tmp_path, monkeypatch):
    d = Database(tmp_path / "arta.sqlite3")
    monkeypatch.setattr(db_mod, "MIGRATIONS", db_mod.MIGRATIONS[:5])
    monkeypatch.setattr(db_mod, "LATEST_VERSION", 5)
    assert d.migrate() == 5
    with d.tx() as c:
        c.execute(
            "INSERT INTO opportunities (id, stage, application_status, applied_at, application_archived, "
            "application_archived_at, saved, name, type, source, created_at) "
            "VALUES ('old', 'analysed', 'approved', 't', 1, 't2', 1, 'n', 'grant', 'f', 't')"
        )
        c.execute(
            "INSERT INTO opportunities (id, stage, filter_reason, name, type, source, created_at) "
            "VALUES ('flt', 'filtered', 'why', 'n', 'grant', 'f', 't')"
        )
        c.execute(
            "INSERT INTO updates (message_id, opportunity_id, sender, received_at, body) "
            "VALUES ('m1', 'old', 's', 't', 'b')"
        )
        c.execute(
            "INSERT INTO sets (id, kind, name, opportunity_id, created_at) VALUES ('s1', 'cv', 'n', 'old', 't')"
        )
    monkeypatch.undo()
    assert d.migrate() == db_mod.LATEST_VERSION >= 6
    old = opportunity.get(d, "old")
    assert old.application == opportunity.Application("approved", "t", True, "t2") and old.saved
    assert opportunity.get(d, "flt").filter_reason == "why"
    with d.read() as c:
        # The rebuild must not have cascaded into updates or nulled the set's link.
        assert c.execute("SELECT COUNT(*) FROM updates WHERE opportunity_id = 'old'").fetchone()[0] == 1
        assert c.execute("SELECT opportunity_id FROM sets WHERE id = 's1'").fetchone()[0] == "old"
        assert c.execute("PRAGMA foreign_key_check").fetchall() == []
        # Same indexes as before, and the CHECKs that were there still are.
        names = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
        assert {"opportunities_stage", "opportunities_application"} <= names
    with d.tx() as c:
        c.execute("UPDATE opportunities SET application_status = 'declined' WHERE id = 'old'")
        with pytest.raises(sqlite3.IntegrityError):
            c.execute("UPDATE opportunities SET application_status = 'waiting' WHERE id = 'old'")
        with pytest.raises(sqlite3.IntegrityError):
            c.execute("UPDATE opportunities SET filter_reason = 'x' WHERE id = 'old'")


def test_upgrade_from_v6_turns_pending_into_applied_and_then_refuses_pending(tmp_path, monkeypatch):
    d = Database(tmp_path / "arta.sqlite3")
    monkeypatch.setattr(db_mod, "MIGRATIONS", db_mod.MIGRATIONS[:6])
    monkeypatch.setattr(db_mod, "LATEST_VERSION", 6)
    assert d.migrate() == 6
    with d.tx() as c:
        for i, status in (("p", "pending"), ("a", "approved")):
            c.execute(
                "INSERT INTO opportunities (id, stage, application_status, applied_at, name, type, source, created_at) "
                f"VALUES ('{i}', 'analysed', '{status}', 't', 'n', 'grant', 'f', 't')"
            )
        c.execute(
            "INSERT INTO updates (message_id, opportunity_id, sender, received_at, body) VALUES ('m1', 'p', 's', 't', 'b')"
        )
    monkeypatch.undo()
    assert d.migrate() == db_mod.LATEST_VERSION >= 7
    assert opportunity.get(d, "p").application.status == "applied"
    assert opportunity.get(d, "p").application.applied_at == "t"
    assert opportunity.get(d, "a").application.status == "approved"
    with d.read() as c:
        assert c.execute("SELECT COUNT(*) FROM updates WHERE opportunity_id = 'p'").fetchone()[0] == 1
        assert c.execute("PRAGMA foreign_key_check").fetchall() == []
    with d.tx() as c:
        with pytest.raises(sqlite3.IntegrityError):
            c.execute("UPDATE opportunities SET application_status = 'pending' WHERE id = 'a'")
