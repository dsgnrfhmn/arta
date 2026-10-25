"""scripts/purge_records_by_source.py: dry run by default, only untouched records go,
a backup is made, intake_seen is kept."""
import importlib.util
from pathlib import Path

import pytest

from core.db import Database
from models import opportunity

spec = importlib.util.spec_from_file_location(
    "purge", Path(__file__).resolve().parent.parent / "scripts" / "purge_records_by_source.py")
purge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(purge)


def rec(i, source, **kw):
    return opportunity.Opportunity(id=i, stage=kw.pop("stage", "for_review"), name=f"n{i}", type="open_call",
                                   source=source, created_at="2026-10-01T00:00:00Z", description="", **kw)


@pytest.fixture
def data_dir(tmp_path):
    db = Database(tmp_path / "arta.sqlite3")
    db.migrate()
    with db.tx() as c:
        opportunity.insert(c, rec("a", "Google"))
        opportunity.insert(c, rec("b", "Google Drive", stage="filtered", filter_reason="x"))
        opportunity.insert(c, rec("c", "Google", notes="my note"))
        opportunity.insert(c, rec("d", "ArtConnect"))
        c.execute("INSERT INTO intake_seen (message_id, seen_at) VALUES ('gmail:1', 'now')")
    return tmp_path


def ids(data_dir):
    import sqlite3
    return {r[0] for r in sqlite3.connect(data_dir / "arta.sqlite3").execute("SELECT id FROM opportunities")}


def test_dry_run_changes_nothing(data_dir, capsys):
    assert purge.main([str(data_dir), "--google"]) == 0
    assert ids(data_dir) == {"a", "b", "c", "d"}
    assert "Dry run" in capsys.readouterr().out


def test_apply_deletes_untouched_google_records_only_and_backs_up(data_dir):
    assert purge.main([str(data_dir), "--google", "--apply"]) == 0
    assert ids(data_dir) == {"c", "d"}                       # c has a note, d is not Google
    assert len(list(data_dir.glob("arta.sqlite3.bak-*"))) == 1
    import sqlite3
    assert sqlite3.connect(data_dir / "arta.sqlite3").execute("SELECT COUNT(*) FROM intake_seen").fetchone()[0] == 1


def test_source_is_an_exact_name(data_dir):
    purge.main([str(data_dir), "--source", "google", "--apply"])
    assert ids(data_dir) == {"b", "c", "d"}                  # "Google Drive" is not "google"


def test_no_database_fails_loud(tmp_path):
    assert purge.main([str(tmp_path), "--google"]) == 2
