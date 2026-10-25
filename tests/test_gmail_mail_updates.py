"""Gmail replies for applied items: the matching rule (pipeline/reply_match.py),
the read-only fetch on a fake Gmail service, intake leaving replies to Updates, and
the wiring in build_mail."""

import base64

import pytest

from core.config import ConfigError, example_data, parse
from core.db import Database
from models.opportunity import Application, Opportunity
from pipeline import intake
from pipeline.reply_match import candidates, match
from sources.gmail_mail_updates import GmailMailUpdates
from sources.intake_source import IncomingOpportunity
from sources.mail_updates import MailError
from web import server as s

APPLIED = "2026-10-01T10:00:00Z"
LATER = "2026-10-05T00:00:00Z"


def opp(id, name, applied_at=APPLIED):
    return Opportunity(
        id=id, stage="analysed", name=name, type="open_call", source="gmail",
        created_at="2026-09-01T00:00:00Z", application=Application("applied", applied_at),
    )


ARTSPACE = opp("o-artspace", "Artspace Residency 2027")
FJORD = opp("o-fjord", "Fjord Arts Mobility Grant")


# --- matching -------------------------------------------------------------------

def test_the_name_in_the_body_matches():
    body = "Dear Jane, thank you for applying to the Artspace residency. We are happy to"
    assert match("Info", "Your application", body, LATER, [ARTSPACE, FJORD]) is ARTSPACE


def test_the_name_in_the_subject_or_sender_matches():
    assert match("Fjord Arts", "Mobility grant: decision", "", LATER, [FJORD]) is FJORD
    assert match("", "Re: Artspace Residency 2027", "", LATER, [ARTSPACE]) is ARTSPACE


def test_without_the_name_nothing_matches():
    assert match("Info", "Thank you", "We received your application.", LATER, [ARTSPACE, FJORD]) is None


def test_most_of_the_distinctive_words_are_needed():
    # "Fjord Arts Mobility Grant" -> {fjord, mobility}: one of two is below the gate.
    assert match("", "x", "a mobility programme", LATER, [FJORD]) is None
    assert match("", "x", "the fjord mobility programme", LATER, [FJORD]) is FJORD


def test_generic_words_alone_do_not_match():
    assert match("", "Open call for artists: residency grant 2027", "apply now, international artists", LATER, [FJORD]) is None


def test_words_match_whole_and_ignore_case():
    assert match("", "x", "ARTSPACE", LATER, [ARTSPACE]) is ARTSPACE
    assert match("", "x", "artspaceberlin", LATER, [ARTSPACE]) is None


def test_a_message_before_the_application_date_is_not_a_reply():
    assert match("", "x", "Artspace", "2026-09-30T23:00:00Z", [ARTSPACE]) is None
    assert match("", "x", "Artspace", "2026-10-01T00:00:00Z", [ARTSPACE]) is ARTSPACE


def test_the_higher_score_wins_and_a_tie_is_not_guessed():
    wide = opp("o-wide", "Artspace Berlin Studio")          # {artspace, berlin, studio}
    assert match("", "x", "Artspace Berlin", LATER, [ARTSPACE, wide]) is ARTSPACE   # 1.0 beats 0.67
    assert match("", "x", "Artspace Berlin studio", LATER, [ARTSPACE, wide]) is None  # 1.0 and 1.0
    twin = opp("o-twin", "Artspace Residency 2026")
    assert len(candidates("", "x", "Artspace", LATER, [ARTSPACE, twin])) == 2
    assert match("", "x", "Artspace", LATER, [ARTSPACE, twin]) is None
    assert match("", "x", "Artspace and Fjord mobility", LATER, [ARTSPACE, FJORD]) is None


def test_a_partial_fit_loses_to_a_full_one():
    wide = opp("o-wide", "Artspace Berlin Studio")
    assert match("", "x", "Artspace Berlin Studio", LATER, [wide, opp("o-k", "Artspace Studio Berlin Fall")]) is wide


# --- fetch on a fake Gmail service ---------------------------------------------

def _b64(text):
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


class FakeGmail:
    """Just enough of users().messages() for list and get; records every call."""

    def __init__(self, messages, fail=None):
        self.store = messages         # id -> (from, subject, epoch_ms, body)
        self.calls = []
        self.fail = fail

    def users(self):
        return self

    def messages(self):
        return self

    def list(self, **kw):
        self.calls.append(("list", kw))
        return _Exec(lambda: {"messages": [{"id": i} for i in self.store]}, self.fail)

    def get(self, userId, id, format, metadataHeaders=None):
        self.calls.append(("get", id, format))
        frm, subject, ms, body = self.store[id]
        payload = {"headers": [{"name": "From", "value": frm}, {"name": "Subject", "value": subject}]}
        if format == "full":
            payload.update(mimeType="text/plain", body={"data": _b64(body)})
        return _Exec(lambda: {"id": id, "internalDate": str(ms), "payload": payload}, None)

    def modify(self, **kw):           # never called: the source is read-only
        raise AssertionError("modify called")


class _Exec:
    def __init__(self, fn, fail):
        self.fn, self.fail = fn, fail

    def execute(self):
        if self.fail:
            raise self.fail
        return self.fn()


MS = lambda iso: int(__import__("datetime").datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000)


def test_fetch_returns_matched_replies_with_body_and_reads_only():
    fake = FakeGmail({
        "m1": ("Office <info@artspace.example>", "Your application", MS("2026-10-05T09:00:00Z"), "Thank you for applying to Artspace."),
        "m2": ("Shop <news@shop.example>", "Sale", MS("2026-10-05T09:00:00Z"), "50% off"),
        "m3": ("Fjord Arts <post@fjord.example>", "Mobility Grant decision", MS("2026-10-06T09:00:00Z"), "Sorry"),
    })
    got = GmailMailUpdates(lambda: fake).fetch([ARTSPACE, FJORD])
    assert [(u.message_id, u.opportunity_id) for u in got] == [("m1", "o-artspace"), ("m3", "o-fjord")]
    assert got[0].sender == "Office" and got[0].body == "Thank you for applying to Artspace."
    assert got[0].received_at == "2026-10-05T09:00:00Z"
    # The query is bounded by the oldest application and the inbox; nothing but list and get.
    assert fake.calls[0][1]["q"] == "in:inbox after:2026/10/01"
    assert {c[0] for c in fake.calls} == {"list", "get"}


def test_fetch_with_no_applications_does_not_touch_the_mailbox():
    fake = FakeGmail({})
    assert GmailMailUpdates(lambda: fake).fetch([]) == []
    assert fake.calls == []


def test_a_mailbox_error_is_one_user_facing_error_without_details():
    class HttpError(Exception):
        pass
    fake = FakeGmail({}, fail=HttpError("token=secret-value"))
    with pytest.raises(MailError) as e:
        GmailMailUpdates(lambda: fake).fetch([ARTSPACE])
    assert "secret" not in str(e.value) and "HttpError" in str(e.value)


def test_a_failing_auth_fails_closed():
    def broken():
        raise RuntimeError("no token")
    with pytest.raises(MailError):
        GmailMailUpdates(broken).fetch([ARTSPACE])


# --- intake leaves replies to Updates --------------------------------------------

class _Mailbox:
    name = "gmail"
    mailbox = True

    def __init__(self, items):
        self.items = items

    def fetch(self, seen):
        return [i for i in self.items if i.message_id not in seen]


class _Listing(_Mailbox):
    name = "listing"
    mailbox = False


def _msg(id, subject, body="", received=LATER):
    return IncomingOpportunity(id, "Someone", subject, received, body)


def test_intake_skips_replies_to_applied_items_and_marks_them_seen(tmp_path):
    db = Database(tmp_path / "arta.sqlite3")
    db.migrate()
    src = _Mailbox([
        _msg("r1", "Your application", "Thank you for applying to Artspace."),
        _msg("n1", "Open call: Lake Studio Residency 2027", "Apply by May."),
        _msg("old", "Artspace Residency 2027 open call", "", received="2026-09-20T00:00:00Z"),
    ])
    got = intake.run(db, [src], LATER, applied=[ARTSPACE])
    assert got["added"] == 2 and got["sources"][0]["replies"] == 1
    with db.read() as c:
        names = {r[0] for r in c.execute("SELECT name FROM opportunities")}
        seen = {r[0] for r in c.execute("SELECT message_id FROM intake_seen")}
    assert names == {"Open call: Lake Studio Residency 2027", "Artspace Residency 2027 open call"}
    assert "gmail:r1" in seen
    assert intake.run(db, [src], LATER, applied=[ARTSPACE])["added"] == 0


def test_intake_keeps_everything_from_a_source_that_is_not_the_mailbox(tmp_path):
    db = Database(tmp_path / "arta.sqlite3")
    db.migrate()
    got = intake.run(db, [_Listing([_msg("l1", "Artspace Residency 2027")])], LATER, applied=[ARTSPACE])
    assert got["added"] == 1 and "replies" not in got["sources"][0]


def test_contact_is_gone_from_the_api_and_the_schema(tmp_path):
    assert "contact" not in ARTSPACE.to_api()["application"]
    db = Database(tmp_path / "arta.sqlite3")
    db.migrate()
    with db.read() as c:
        assert "application_contact" not in {r[1] for r in c.execute("PRAGMA table_info(opportunities)")}


# --- wiring ------------------------------------------------------------------------

def test_updates_source_is_chosen_explicitly(tmp_path):
    def conf(updates):
        data = example_data()
        data["server"]["data_dir"] = str(tmp_path)
        data["updates"]["source"] = updates
        data["google"]["token_file"] = "/srv/token.json"
        return parse(data)
    assert conf("none").updates is None and s.build_mail(conf("none")) is None
    assert type(s.build_mail(conf("fixtures"))).__name__ == "FixtureMailUpdates"
    assert type(s.build_mail(conf("gmail"))).__name__ == "GmailMailUpdates"
    with pytest.raises(ConfigError):
        conf("imap")
