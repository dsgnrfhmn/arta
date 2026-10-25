"""Intake pre-filter: sender and subject rules from config/intake_filters.json,
matched items land in Filtered with the rule's reason, and are never re-read."""

import json
from pathlib import Path

import pytest

from core.config import REPO_ROOT
from core.db import Database
from models import opportunity
from pipeline import intake
from pipeline.intake_filter import FilterConfigError, Verdict, load, parse
from sources.intake_source import IncomingOpportunity

NOW = "2026-10-10T12:00:00Z"
SHIPPED = REPO_ROOT / "config" / "intake_filters.json"


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "arta.sqlite3")
    d.migrate()
    return d


class Source:
    name = "test"

    def __init__(self, *items):
        self.items = items

    def fetch(self, seen):
        return [i for i in self.items if i.message_id not in seen]


def item(mid, subject, address="calls@gallery.example", body="Some text."):
    return IncomingOpportunity(mid, address, subject, "2026-10-09T08:00:00Z", body, sender_address=address)


def rules(*r, accept=()):
    return parse({"rules": list(r), "force_accept_substrings": list(accept)})


def reason(f, address, subject):
    v = f.check(address, subject)
    return v.reason if v else None


# --- matching ----------------------------------------------------------------

def test_sender_domain_matches_the_domain_and_its_subdomains_not_look_alikes():
    f = rules({"reason": "Google", "sender_domains": ["google.com"]})
    assert f.check("no-reply@accounts.google.com", "Security alert")
    assert f.check("a@google.com", "x")
    assert f.check("a@notgoogle.com", "x") is None
    assert f.check("a@google.com.evil.example", "x") is None
    assert f.check("", "x") is None                       # a source without an address is never sender-matched


def test_exact_sender_address():
    f = rules({"reason": "One sender", "sender_addresses": ["Spam@Example.com"]})
    assert reason(f, "spam@example.com", "x") == "One sender (sender spam@example.com)"
    assert f.check("other@example.com", "x") is None


def test_subject_words_are_whole_words_substrings_are_not():
    f = rules({"reason": "Music", "subject_words": ["music", "opera"], "subject_substrings": ["orchestr"]})
    assert reason(f, "a@b.example", "Call for Music residents") == "Music (subject: music)"
    assert reason(f, "a@b.example", "Music-based art") == "Music (subject: music)"
    assert f.check("a@b.example", "Operation Gallery") is None          # not "opera" inside a word
    assert f.check("a@b.example", "Museum of Musicality") is None
    assert reason(f, "a@b.example", "Open call: orchestral sound and space") == "Music (subject: orchestr)"


def test_the_reason_is_built_from_the_rule_not_from_the_email():
    f = rules({"reason": "Music", "subject_substrings": ["orchestr"]})
    assert reason(f, "a@b.example", "Open call <script>Orchestrafest</script>") == "Music (subject: orchestr)"


# --- config is validated, fail loud ------------------------------------------

@pytest.mark.parametrize("bad", [
    [],
    {"rules": []},
    {"rules": [{"subject_words": ["x"]}]},                                   # no reason
    {"rules": [{"reason": "r"}]},                                            # would do nothing
    {"rules": [{"reason": "r", "subject_words": [""]}]},                     # empty term matches everything
    {"rules": [{"reason": "r", "subject_words": "music"}]},                  # not a list
    {"rules": [{"reason": "r", "subject_wrds": ["music"]}]},                 # typo
    {"rules": [{"reason": "r", "sender_domains": ["a@b.com"]}]},
    {"rules": [{"reason": "r", "sender_addresses": ["b.com"]}]},
    {"rules": [], "extra": 1},
    {"rules": [{"reason": "r", "action": "delete", "subject_words": ["x"]}]},      # unknown action
    {"rules": [{"reason": "r", "subject_words": ["x"]}], "force_accept_substrings": "fine art"},
    {"rules": [{"reason": "r", "subject_words": ["x"]}], "force_accept_substrings": [""]},
])
def test_a_broken_config_is_refused(bad):
    with pytest.raises(FilterConfigError):
        parse(bad)


def test_missing_and_invalid_files_are_refused(tmp_path):
    with pytest.raises(FilterConfigError):
        load(tmp_path / "nope.json")
    p = tmp_path / "f.json"
    p.write_text("{not json")
    with pytest.raises(FilterConfigError):
        load(p)


def test_the_shipped_config_loads_and_does_something():
    f = load(SHIPPED)
    assert f.check("no-reply@accounts.google.com", "Security alert") == Verdict("drop", "Google account notice (sender domain google.com)")
    assert f.check("news@artconnect.example", "Open call: Orchestra residency").action == "filter"
    assert f.check("news@artconnect.example", "Open call: Coastal Residency 2027") is None
    assert f.check("news@artconnect.example", "Fine Art residency for composers") is None   # force-accept


# --- in the pipeline ---------------------------------------------------------

def test_matched_items_land_in_filtered_with_a_reason_and_the_rest_in_for_review(db):
    f = rules({"reason": "Field: music", "subject_words": ["music"]})
    src = Source(item("1", "Open call: music residency"), item("2", "Open call: painting residency"))
    result = intake.run(db, [src], NOW, f)
    assert result == {"added": 1, "filtered": 1, "sources": [{"name": "test", "added": 1, "filtered": 1}]}
    filtered = opportunity.list_all(db, stage="filtered")
    assert [(o.name, o.filter_reason) for o in filtered] == [
        ("Open call: music residency", "Field: music (subject: music)")]
    assert [o.name for o in opportunity.list_all(db, stage="for_review")] == ["Open call: painting residency"]
    assert filtered[0].body == "Some text."               # kept, so promoting it loses nothing


def test_a_filtered_item_is_not_judged_again_and_can_be_promoted(db):
    f = rules({"reason": "Field: music", "subject_words": ["music"]})
    src = Source(item("1", "Music call"))
    intake.run(db, [src], NOW, f)
    assert intake.run(db, [src], NOW, f)["filtered"] == 0
    assert intake.run(db, [src], NOW, None)["added"] == 0         # even with the filter removed
    (rec,) = opportunity.list_all(db, stage="filtered")
    with db.tx() as conn:
        opportunity.promote(conn, [rec.id])
    (rec,) = opportunity.list_all(db, stage="for_review")
    assert rec.filter_reason is None


def test_without_filters_nothing_is_filtered(db):
    result = intake.run(db, [Source(item("1", "Music call"))], NOW)
    assert (result["added"], result["filtered"]) == (1, 0)


def test_the_log_carries_ids_and_reasons_not_subjects(db, caplog):
    f = rules({"reason": "Field: music", "subject_words": ["music"]})
    with caplog.at_level("INFO", logger="arta.intake"):
        intake.run(db, [Source(item("m-77", "Secret music subject line"))], NOW, f)
    text = caplog.text
    assert "m-77" in text and "Field: music" in text
    assert "Secret" not in text and "subject line" not in text


# --- drop and force-accept ---------------------------------------------------

def test_force_accept_lets_a_subject_through_filter_rules_but_not_look_alikes():
    f = rules({"reason": "Music", "subject_words": ["music", "composer"]}, accept=["fine art"])
    assert reason(f, "a@b.example", "Fine Art and music: open call") is None
    assert reason(f, "a@b.example", "Fine Arts residency for a composer") is None
    assert reason(f, "a@b.example", "Redefine art with music") == "Music (subject: music)"   # "fine art" inside a word


def test_force_accept_does_not_override_drop():
    f = rules({"reason": "Google", "action": "drop", "sender_domains": ["google.com"]}, accept=["fine art"])
    assert f.check("noreply@google.com", "Fine art shared with you") == Verdict("drop", "Google (sender domain google.com)")


def test_a_dropped_item_is_stored_nowhere_and_not_read_again(db):
    f = rules({"reason": "Google", "action": "drop", "sender_domains": ["google.com"]},
              {"reason": "Music", "subject_words": ["music"]})
    src = Source(item("1", "Security alert", "no-reply@accounts.google.com"),
                 item("2", "Music call"), item("3", "Painting call"))
    result = intake.run(db, [src], NOW, f)
    assert result == {"added": 1, "filtered": 1, "dropped": 1,
                      "sources": [{"name": "test", "added": 1, "filtered": 1, "dropped": 1}]}
    names = {o.name for o in opportunity.list_all(db)}
    assert names == {"Music call", "Painting call"}          # the dropped one is in no stage
    with db.read() as c:
        assert c.execute("SELECT COUNT(*) FROM opportunities WHERE body LIKE '%Security%' OR name LIKE '%Security%'").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM intake_seen WHERE message_id = 'test:1'").fetchone()[0] == 1
    assert intake.run(db, [src], NOW, f)["added"] == 0 and "dropped" not in intake.run(db, [src], NOW, f)


def test_the_dropped_log_line_has_the_id_and_reason_only(db, caplog):
    f = rules({"reason": "Google", "action": "drop", "sender_domains": ["google.com"]})
    with caplog.at_level("INFO", logger="arta.intake"):
        intake.run(db, [Source(item("g-9", "Private security subject", "a@google.com"))], NOW, f)
    assert "g-9" in caplog.text and "Google" in caplog.text and "Private" not in caplog.text
