"""Intake: sources behind one interface, one item = one For Review record,
deduplication in intake_seen, sanitized text, the endpoint. No network: the Gmail
source runs against a fake service that can only list and read."""

import base64
import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from core.config import Config
from core.db import Database
from models import opportunity
from pipeline import intake
from sources.fixture_intake import FixtureIntake
from sources.intake_source import IncomingOpportunity, IntakeError
from sources.opencall_email_source import GmailIntake, message_text
from web import server as s

FIXTURES = Path(__file__).resolve().parent / "fixtures"
NOW = "2026-10-06T12:00:00Z"


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "arta.sqlite3")
    d.migrate()
    return d


def b64(text):
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


class FakeGmail:
    """Only users().messages().list/get exist; anything else (modify, send, trash)
    is an AttributeError, so a write would fail the test."""

    def __init__(self, store):
        self.store = store            # id -> full message, newest first
        self.reads = []

    def users(self):
        return self

    def messages(self):
        return self

    def list(self, userId, maxResults, pageToken=None):
        return _Exec({"messages": [{"id": i} for i in self.store]})

    def get(self, userId, id, format):
        self.reads.append(id)
        return _Exec(self.store[id])


class _Exec:
    def __init__(self, value):
        self.value = value

    def execute(self):
        return self.value


def gmail_message(subject, body, sender="ArtConnect <news@artconnect.example>", html=None):
    parts = [{"mimeType": "text/plain", "body": {"data": b64(body)}}] if body else []
    if html:
        parts.append({"mimeType": "text/html", "body": {"data": b64(html)}})
    return {"internalDate": "1790000000000", "payload": {
        "headers": [{"name": "Subject", "value": subject}, {"name": "From", "value": sender}],
        "mimeType": "multipart/alternative", "parts": parts}}


# --- pipeline ----------------------------------------------------------------

def test_fixture_emails_become_for_review_records_and_a_second_run_adds_nothing(db):
    sources = [FixtureIntake(FIXTURES / "emails.json")]
    first = intake.run(db, sources, NOW)
    assert first == {"added": 4, "filtered": 0, "sources": [{"name": "fixtures", "added": 4, "filtered": 0}]}
    rows = [o for o in opportunity.list_all(db, stage="for_review") if o.source == "calls@example.invalid" or o.body]
    assert len(rows) >= 4
    assert intake.run(db, sources, NOW)["added"] == 0


def test_a_record_is_for_review_with_empty_description_and_the_text_kept_for_analysis(db):
    intake.run(db, [FixtureIntake(FIXTURES / "emails.json")], NOW)
    rec = next(o for o in opportunity.list_all(db, stage="for_review") if o.name == "Open call: Coastal Residency 2027")
    assert (rec.stage, rec.type, rec.description, rec.link) == ("for_review", "open_call", "", None)
    assert "two month residency" in rec.body
    assert "body" not in rec.to_api()                 # internal: never sent to the page


def test_subject_and_body_are_sanitized(db):
    intake.run(db, [FixtureIntake(FIXTURES / "emails.json")], NOW)
    rec = next(o for o in opportunity.list_all(db, stage="for_review") if "Project grant" in o.name)
    assert "<b>" not in rec.name and "<script" not in rec.name and "alert(1)" not in rec.name


@pytest.mark.parametrize("url,ok", [
    ("https://www.artconnect.com/opportunity/abc", True),
    ("https://example.org:443/x?a=1#f", True),
    ("http://example.org/x", False),
    ("javascript:alert(1)", False),
    ("data:text/html,<script>alert(1)</script>", False),
    ("//example.org/x", False),
    ("https://user:pw@example.org/x", False),
    ("https://example.org:8443/x", False),
    ("https:///nohost", False),
    ("https://example.org/a b", False),
    ("https://example.org/\nx", False),
    ("https://example.org/" + "a" * 2100, False),
])
def test_link_validation(url, ok):
    assert (intake._link(url) == url) is ok


def test_an_invalid_link_is_dropped_and_the_record_is_kept(db):
    class Web:
        name = "web"

        def fetch(self, seen):
            return [
                IncomingOpportunity("1", "site", "Good", NOW, "t", link="https://example.org/ok"),
                IncomingOpportunity("2", "site", "Bad", NOW, "t", link="javascript:alert(1)"),
                IncomingOpportunity("3", "site", "None", NOW, "t"),
            ]
    intake.run(db, [Web()], NOW)
    by_name = {o.name: o for o in opportunity.list_all(db, stage="for_review")}
    assert by_name["Good"].link == "https://example.org/ok"
    assert by_name["Bad"].link is None and by_name["None"].link is None


def test_seen_ids_are_per_source(db):
    class Other:
        name = "other"

        def fetch(self, seen):
            return [IncomingOpportunity("email-0001", "x@y.example", "Same id, other source", NOW, "text")]
    intake.run(db, [FixtureIntake(FIXTURES / "emails.json"), Other()], NOW)
    with db.read() as c:
        ids = {r[0] for r in c.execute("SELECT message_id FROM intake_seen")}
    assert {"fixtures:email-0001", "other:email-0001"} <= ids


def test_a_source_that_fails_stops_the_run_and_stores_nothing_from_it(db):
    class Broken:
        name = "broken"

        def fetch(self, seen):
            raise IntakeError("The mailbox could not be read.")
    with pytest.raises(IntakeError):
        intake.run(db, [Broken()], NOW)
    with db.read() as c:
        assert c.execute("SELECT COUNT(*) FROM intake_seen").fetchone()[0] == 0


# --- Gmail source -----------------------------------------------------------

def test_gmail_reads_only_unseen_messages_and_never_writes():
    box = FakeGmail({"m2": gmail_message("Second", "two"), "m1": gmail_message("First", "one")})
    items = GmailIntake(lambda: box).fetch({"m1"})
    assert [i.message_id for i in items] == ["m2"] and box.reads == ["m2"]
    assert items[0].sender == "ArtConnect" and items[0].subject == "Second" and items[0].body == "two"
    with pytest.raises(AttributeError):
        box.users().messages().modify


def test_html_only_message_gives_its_visible_text():
    html = "<html><style>p{color:red}</style><body><p>Hello <b>artists</b></p><script>steal()</script><p>Apply by May</p></body></html>"
    text = message_text(gmail_message("S", "", html=html)["payload"])
    assert "Hello artists" in text and "Apply by May" in text
    assert "steal" not in text and "color:red" not in text


def test_gmail_failure_is_one_user_facing_error_without_the_original_message():
    def factory():
        raise RuntimeError("secret-token-abc123")
    with pytest.raises(IntakeError) as e:
        GmailIntake(factory).fetch(set())
    assert "secret-token" not in str(e.value) and "RuntimeError" in str(e.value)


def test_gmail_auth_error_keeps_its_own_user_facing_message():
    class GoogleAuthError(Exception):
        pass

    def factory():
        raise GoogleAuthError("The token was revoked. Reconnect the account.")
    with pytest.raises(IntakeError, match="revoked"):
        GmailIntake(factory).fetch(set())


def test_logs_carry_message_ids_not_subjects_or_bodies(db, caplog):
    box = FakeGmail({"m9": gmail_message("A very private subject", "a very private body")})
    with caplog.at_level("INFO", logger="arta.intake"):
        intake.run(db, [GmailIntake(lambda: box)], NOW)
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "m9" in logged and "private" not in logged


# --- HTTP -------------------------------------------------------------------

@pytest.fixture
def srv(db, tmp_path):
    cfg = Config(updates="fixtures", projects="fixtures", cv="fixtures", llm="mock", port=0, data_dir=tmp_path / "data", intake=("fixtures",))
    server = s.make_server(cfg, db, port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()
    server.server_close()


def post(srv):
    req = urllib.request.Request(f"http://127.0.0.1:{srv.server_address[1]}/api/intake/run", method="POST",
                                 data=b"{}", headers={"Content-Type": "application/json"})
    if not req.has_header("Origin"):   # a browser sends it on every write
        req.add_header("Origin", "http://" + req.host)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_endpoint_reports_counts_and_is_idempotent(srv):
    assert post(srv) == (200, {"added": 4, "filtered": 0, "sources": [{"name": "fixtures", "added": 4, "filtered": 0}]})
    assert post(srv)[1]["added"] == 0


def test_endpoint_without_a_configured_source_says_so(db, tmp_path):
    cfg = Config(updates="fixtures", projects="fixtures", cv="fixtures", llm="mock", port=0, data_dir=tmp_path / "data")
    server = s.make_server(cfg, db, port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        status, body = post(server)
    finally:
        server.shutdown()
        server.server_close()
    assert status == 503 and "intake.sources" in body["error"]["message"]


def test_endpoint_maps_a_source_error_to_502(db, tmp_path):
    class Broken:
        name = "broken"

        def fetch(self, seen):
            raise IntakeError("The mailbox could not be read.")
    cfg = Config(updates="fixtures", projects="fixtures", cv="fixtures", llm="mock", port=0, data_dir=tmp_path / "data")
    server = s.make_server(cfg, db, port=0, intake_sources=[Broken()])
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        status, body = post(server)
    finally:
        server.shutdown()
        server.server_close()
    assert status == 502 and body["error"]["code"] == "source_error"


def test_build_intake_keeps_the_configured_order(tmp_path):
    cfg = Config(updates="fixtures", projects="fixtures", cv="fixtures", llm="mock", port=0, data_dir=tmp_path / "data", intake=("gmail", "fixtures"))
    built = s.build_intake(cfg)
    assert [type(src) for src in built] == [GmailIntake, FixtureIntake]
    assert s.build_intake(Config(updates="fixtures", projects="fixtures", cv="fixtures", llm="mock", port=0, data_dir=tmp_path / "data")) is None
