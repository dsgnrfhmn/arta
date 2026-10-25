"""The dashboard's server: the pages (web/static.py) and the JSON API under /api/.
stdlib only, binds loopback only (core.config.HOST), no TLS, no sign-in: it is opened
from a bookmark on the same machine.

Rules kept in one place (_dispatch), before any route or file is looked at, so
handlers cannot forget them:
- Host must be 127.0.0.1:<port> or localhost:<port>, else 403. A page on another site
  that rebinds its own name to 127.0.0.1 (DNS rebinding) sends its own name as Host.
- Every write (POST, PUT, PATCH, DELETE) must carry an Origin of this server, else
  403. Browsers send Origin on every write, so a page on another site cannot trigger
  an action (CSRF), and a client that sends none is refused rather than trusted.
- No CORS headers, ever: another origin can neither read an answer nor preflight.
- Writes must be application/json; request bodies are capped.
- Errors are JSON {"error": {"code", "message"}}. Unexpected exceptions log a
  traceback server-side and return a generic 500; nothing internal leaks.
- Nothing logs a body, only method, path, status.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import re
import signal
import sys
import threading
from datetime import datetime, timezone
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, quote, unquote, urlsplit

from core import config as config_mod
from core.config import HOST, REPO_ROOT, Config, ConfigError
from core.db import Database, DbError
from core import errors
from core import artist_profile
from core.claude_client import Client, ClientError, make_client
from models import opportunity, update
from pipeline import adapt_cv, cost_estimate, edit_cv, edit_portfolio, intake, opencall_summarise
from sets import cv_sets, portfolio_sets, prepare, store
from sources import project_source as ps
from sources.cv_source import CvSource
from sources.fixture_mail_updates import FixtureMailUpdates
from sources.gmail_mail_updates import GmailMailUpdates
from sources.mail_updates import MailError, MailUpdates
from sources.drive_cv import DriveCv
from sources.portfolio_cover import DriveCover, FileCover, load_cover_text
from sources.local_cv import LocalCv
from sources.fixture_intake import FixtureIntake
from sources.artconnect_source import ArtConnectSource
from pipeline.intake_filter import FilterConfigError, IntakeFilters, load as load_intake_filters
from sources.intake_source import IntakeError, IntakeSource
from sources.opencall_email_source import GmailIntake
from sources.drive_projects import DriveProjects
from sources.local_projects import LocalProjects
from sources.image_meta import load_registry
from sources.thumb_cache import ThumbCache, ThumbError
from web.exports import Exports, filename as export_filename
from web.jobs import Jobs
from web import static

log = logging.getLogger("arta.web")

MAX_BODY = 1_000_000


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


@dataclass
class Context:
    config: Config
    db: Database
    projects: "ps.ProjectSource | None" = None
    thumbs: "ThumbCache | None" = None
    cv: "CvSource | None" = None
    mail: "MailUpdates | None" = None
    client: "Client | None" = None
    intake: "list[IntakeSource] | None" = None   # None: not configured
    intake_filters: "IntakeFilters | None" = None   # None: nothing is filtered
    profile: str | None = None   # the artist profile text (core/artist_profile.py); None: none configured
    cover: "DriveCover | FileCover | None" = None   # page 1 of the portfolio PDF; None: no cover
    jobs: "Jobs | None" = None   # the worker process for HEAVY handlers (web/jobs.py); None: inline
    exports: "Exports | None" = None   # PDF exports (web/exports.py); None: not available


@dataclass
class Binary:
    """A non-JSON reply. Only the thumbnail endpoint uses it."""
    content_type: str
    data: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class Request:
    ctx: Context
    method: str
    path: str
    params: dict[str, str]
    query: dict[str, list[str]]
    body: Any = None
    headers: Any = field(default_factory=dict)

    def q(self, name: str) -> str | None:
        vals = self.query.get(name)
        return vals[-1] if vals else None


Handler = Callable[[Request], "tuple[int, Any]"]


@dataclass
class Router:
    routes: list[tuple[str, re.Pattern[str], Handler]] = field(default_factory=list)

    def add(self, method: str, pattern: str, handler: Handler) -> None:
        # "/api/opportunities/{id}" -> named groups
        rx = re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", pattern)
        self.routes.append((method, re.compile(f"^{rx}$"), handler))

    def match(self, method: str, path: str) -> "tuple[Handler, dict[str, str]]":
        path_matched = False
        for m, rx, handler in self.routes:
            hit = rx.match(path)
            if not hit:
                continue
            if m == method:
                # Matched on the encoded path, so an encoded "/" never splits a segment;
                # decoded after, because clients encode ids ("set:abc" is sent as set%3Aabc).
                return handler, {k: unquote(v) for k, v in hit.groupdict().items()}
            path_matched = True
        if path_matched:
            raise ApiError(405, "method_not_allowed", f"{method} is not allowed on {path}.")
        raise ApiError(404, "not_found", f"No route for {path}.")


# --- handlers ---------------------------------------------------------------

def _unread(req: Request) -> int:
    with req.ctx.db.read() as conn:
        return update.unread_total(conn)


def _sources(cfg: Config) -> "dict[str, Any]":
    """Where each kind of content comes from, as configured ("none" for not configured)."""
    return {"intake": list(cfg.intake or []), "updates": cfg.updates or "none",
            "projects": cfg.projects or "none", "cv": cfg.cv or "none"}


def health(req: Request) -> "tuple[int, Any]":
    return 200, {
        "status": "ok",
        "sources": _sources(req.ctx.config),
        "llm": req.ctx.config.llm,  # "mock" until dsgnrfhmn opts in; the UI labels it
        "schema_version": req.ctx.db.version(),
        "unread": _unread(req),   # any unread reply: the nav dot on every page
    }


def _flag(req: Request, name: str) -> bool | None:
    raw = req.q(name)
    if raw is None:
        return None
    if raw not in ("true", "false"):
        raise ApiError(400, "bad_request", f"{name} must be true or false.")
    return raw == "true"


def list_opportunities(req: Request) -> "tuple[int, Any]":
    try:
        items = opportunity.list_all(
            req.ctx.db, req.q("stage"), _flag(req, "archived"), _flag(req, "applied"), _flag(req, "tracker_archived")
        )
    except ValueError as e:
        raise ApiError(400, "bad_request", str(e)) from None
    return 200, {"items": [o.to_api() for o in items]}


def get_opportunity(req: Request) -> "tuple[int, Any]":
    o = opportunity.get(req.ctx.db, req.params["id"])
    if o is None:
        raise ApiError(404, "not_found", "No such opportunity.")
    return 200, o.to_api()


def _write(req: Request, fn: "Callable[[Any], Any]") -> Any:
    """Run a model write in one transaction and map its errors to HTTP."""
    try:
        with req.ctx.db.tx() as conn:
            return fn(conn)
    except (errors.NotFound, ps.NotFound) as e:
        raise ApiError(404, "not_found", str(e)) from None
    except errors.Conflict as e:
        raise ApiError(409, "conflict", str(e)) from None
    except ps.SourceError as e:
        raise ApiError(502, "source_error", str(e)) from None
    except ValueError as e:
        raise ApiError(400, "bad_request", str(e)) from None


def _body_ids(req: Request) -> list[str]:
    if not isinstance(req.body, dict):
        raise ApiError(400, "bad_request", "Body must be an object with ids.")
    return req.body.get("ids")  # validated by the model


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _items(rows: "list[opportunity.Opportunity]") -> "tuple[int, Any]":
    return 200, {"items": [o.to_api() for o in rows]}


def patch_opportunity(req: Request) -> "tuple[int, Any]":
    return 200, _write(req, lambda c: opportunity.update_fields(c, req.params["id"], req.body)).to_api()


def archive_opportunities(req: Request) -> "tuple[int, Any]":
    ids = _body_ids(req)
    return _items(_write(req, lambda c: opportunity.archive(c, ids, _now())))


def unarchive_opportunities(req: Request) -> "tuple[int, Any]":
    ids = _body_ids(req)
    return _items(_write(req, lambda c: opportunity.unarchive(c, ids)))


def promote_opportunities(req: Request) -> "tuple[int, Any]":
    ids = _body_ids(req)
    return _items(_write(req, lambda c: opportunity.promote(c, ids)))


def save_opportunities(req: Request) -> "tuple[int, Any]":
    ids = _body_ids(req)
    return _items(_write(req, lambda c: opportunity.set_saved(c, ids, req.body.get("saved"))))


def _projects(req: Request) -> "ps.ProjectSource":
    if req.ctx.projects is None:
        raise ApiError(503, "unavailable", "No project source is configured.")
    return req.ctx.projects


def _source_call(fn: "Callable[[], Any]") -> Any:
    try:
        return fn()
    except ps.NotFound as e:
        raise ApiError(404, "not_found", str(e)) from None
    except ps.SourceError as e:
        raise ApiError(502, "source_error", str(e)) from None
    except ThumbError as e:
        raise ApiError(422, "unprocessable", str(e)) from None


def list_projects(req: Request) -> "tuple[int, Any]":
    projects = _source_call(_projects(req).list_projects)
    return 200, {"items": [p.summary_api() for p in projects]}


def refresh_projects(req: Request) -> "tuple[int, Any]":
    """Read the projects anew from their source (Drive: listing and texts).
    The page calls it when Add project or Create new set is pressed; nothing else does.
    A source without a cache of its own (the fixture tree) is simply listed."""
    source = _projects(req)
    refresh = getattr(source, "refresh", source.list_projects)
    projects = _source_call(refresh)
    return 200, {"items": [p.summary_api() for p in projects]}


def get_project(req: Request) -> "tuple[int, Any]":
    return 200, _source_call(lambda: _projects(req).get_project(req.params["id"])).to_api()


def get_thumb(req: Request) -> "tuple[int, Any]":
    """Cached thumbnail bytes. Never an original. Revalidated with an ETag that
    follows the original's fingerprint, so an edited original is not shown stale."""
    source, cache = _projects(req), req.ctx.thumbs
    if cache is None:
        raise ApiError(503, "unavailable", "No thumbnail cache is configured.")
    pid, iid = req.params["project_id"], req.params["image_id"]
    tag = _source_call(lambda: cache.etag(source, pid, iid))
    headers = {"ETag": f'"{tag}"', "Cache-Control": "private, no-cache"}
    if req.headers.get("If-None-Match") == headers["ETag"]:
        return 304, Binary("image/jpeg", b"", headers)
    path = _source_call(lambda: cache.get(source, pid, iid))
    return 200, Binary("image/jpeg", path.read_bytes(), headers)


# --- sets: what both kinds share; portfolio and CV content below --------------------------------------------------------

def _set_body(req: Request) -> dict:
    if not isinstance(req.body, dict):
        raise ApiError(400, "bad_request", "Body must be an object.")
    return req.body


def _describe(req: Request, record) -> Any:
    if record.kind == "cv":
        return cv_sets.describe(record)
    return portfolio_sets.describe(record, req.ctx.projects, has_cover=req.ctx.cover is not None)


# The cover sheet that opens every portfolio set (dsgnrfhmn, 2026-10-24): page 1 of the
# artist's portfolio PDF. A cover that cannot be read is a "missing" cover sheet with a
# note, never a stale one and never a broken preview.
def _cover_sheet(req: Request) -> "dict[str, Any] | None":
    if req.ctx.cover is None:
        return None
    try:
        cover = req.ctx.cover.current()
        sheet = {"layout": "cover", "cover": cover.fingerprint, "photo": cover.photo is not None}
        if req.ctx.config.cover_text:
            sheet["texts"] = load_cover_text(req.ctx.config.cover_text)
        return sheet
    except ps.SourceError as e:
        return {"layout": "cover", "cover": None, "note": str(e)}


def get_cover(req: Request) -> "tuple[int, Any]":
    """Page 1 of the portfolio PDF as a JPEG, revalidated by its fingerprint."""
    if req.ctx.cover is None:
        raise ApiError(404, "not_found", "No portfolio cover is configured.")
    cover = _source_call(req.ctx.cover.current)
    headers = {"ETag": f'"{cover.fingerprint}"', "Cache-Control": "private, no-cache"}
    if req.headers.get("If-None-Match") == headers["ETag"]:
        return 304, Binary("image/jpeg", b"", headers)
    return 200, Binary("image/jpeg", cover.path.read_bytes(), headers)


def get_cover_photo(req: Request) -> "tuple[int, Any]":
    """The photo embedded in page 1 of the portfolio PDF, as stored there."""
    if req.ctx.cover is None:
        raise ApiError(404, "not_found", "No portfolio cover is configured.")
    cover = _source_call(req.ctx.cover.current)
    if cover.photo is None:
        raise ApiError(404, "not_found", "Page 1 of the portfolio PDF has no photo.")
    headers = {"ETag": f'"{cover.fingerprint}-photo"', "Cache-Control": "private, no-cache"}
    if req.headers.get("If-None-Match") == headers["ETag"]:
        return 304, Binary("image/jpeg", b"", headers)
    return 200, Binary("image/jpeg", cover.photo.read_bytes(), headers)


def _set_out(req: Request, record) -> "tuple[int, Any]":
    return 200, _describe(req, record)


def _set_read(req: Request, fn: "Callable[[Any], Any]") -> Any:
    try:
        with req.ctx.db.read() as conn:
            return fn(conn)
    except errors.NotFound as e:
        raise ApiError(404, "not_found", str(e)) from None


def list_set(req: Request) -> "tuple[int, Any]":
    records = _set_read(req, lambda c: store.list_sets(c, req.params["kind"]))
    count = lambda r: len(portfolio_sets.entries(r.content)) if r.kind == portfolio_sets.KIND else len(r.content)
    return 200, {"items": [{**r.summary_api(), "entry_count": count(r)} for r in records]}


def order_sets(req: Request) -> "tuple[int, Any]":
    """The user's own sidebar order: the ids of the sets shown, top to bottom."""
    ids = _set_body(req).get("ids")
    _write(req, lambda c: store.reorder_sidebar(c, req.params["kind"], ids))
    return list_set(req)


def get_set(req: Request) -> "tuple[int, Any]":
    return _set_out(req, _set_read(req, lambda c: store.get(c, req.params["kind"], req.params["id"])))


def create_set(req: Request) -> "tuple[int, Any]":
    name = _set_body(req).get("name")
    record = _write(req, lambda c: store.create(c, req.params["kind"], _now(), name))
    return 201, _describe(req, record)


def create_full_portfolio_set(req: Request) -> "tuple[int, Any]":
    """New Full Set: every project, all pictures, full text. The projects are read anew
    first, as for Create new set (dsgnrfhmn, 2026-10-22)."""
    _set_body(req)
    source = _projects(req)
    projects = _source_call(getattr(source, "refresh", source.list_projects))
    record = _write(req, lambda c: portfolio_sets.create_full(c, projects, _now()))
    return 201, _describe(req, record)


def patch_set(req: Request) -> "tuple[int, Any]":
    body = _set_body(req)
    kind, set_id = req.params["kind"], req.params["id"]
    if set(body) == {"saved"}:
        return _set_out(req, _write(req, lambda c: store.set_saved(c, kind, set_id, body["saved"], _now())))
    if set(body) == {"name"}:
        return _set_out(req, _write(req, lambda c: store.rename(c, kind, set_id, body["name"])))
    raise ApiError(400, "bad_request", "Send exactly one of {saved} or {name}; use /save to name and keep a set.")


def save_set(req: Request) -> "tuple[int, Any]":
    name = _set_body(req).get("name")
    return _set_out(req, _write(req, lambda c: store.save(c, req.params["kind"], req.params["id"], name, _now())))


def duplicate_set(req: Request) -> "tuple[int, Any]":
    name = _set_body(req).get("name")
    record = _write(req, lambda c: store.duplicate(c, req.params["kind"], req.params["id"], name, _now()))
    return 201, _describe(req, record)


def archive_set(req: Request) -> "tuple[int, Any]":
    _set_body(req)
    return _set_out(req, _write(req, lambda c: store.archive(c, req.params["kind"], req.params["id"], _now())))


def unarchive_set(req: Request) -> "tuple[int, Any]":
    _set_body(req)
    return _set_out(req, _write(req, lambda c: store.unarchive(c, req.params["kind"], req.params["id"])))


def delete_set(req: Request) -> "tuple[int, Any]":
    _write(req, lambda c: store.delete(c, req.params["kind"], req.params["id"]))
    return 200, {"deleted": req.params["id"]}


# --- PDF export (web/exports.py, web/pdf_export.py) ---------------------------------------

def _exports(req: Request) -> "Exports":
    if req.ctx.exports is None:
        raise ApiError(503, "unavailable", "PDF export is not available here.")
    return req.ctx.exports


def start_pdf(req: Request) -> "tuple[int, Any]":
    """Download PDF: start the export of this set and answer its id at once."""
    _set_body(req)
    exports = _exports(req)
    record = _set_read(req, lambda c: store.get(c, req.params["kind"], req.params["id"]))
    job = exports.start(record.kind, record.id, record.name)
    return 202, {"job": job.id, **job.state()}


def _export(req: Request):
    job = _exports(req).get(req.params["job"])
    if job is None:
        raise ApiError(404, "not_found", "This PDF is no longer here. Download it again.")
    return job


def pdf_state(req: Request) -> "tuple[int, Any]":
    return 200, _export(req).state()


def stop_pdf(req: Request) -> "tuple[int, Any]":
    """The toast's close button, after a confirmation: stop this export."""
    job = _exports(req).stop(req.params["job"])
    if job is None:
        raise ApiError(404, "not_found", "This PDF is no longer here.")
    return 200, job.state()


def list_exports(req: Request) -> "tuple[int, Any]":
    """The exports a page should follow or offer, whichever page started them."""
    return 200, {"items": [job.summary() for job in _exports(req).list()]}


def pdf_file(req: Request) -> "tuple[int, Any]":
    job = _export(req)
    if job.state()["status"] != "done" or not job.path.is_file():
        raise ApiError(409, "conflict", "The PDF is not ready.")
    job.downloaded = True
    name = export_filename(job.name)
    ascii_name = name.encode("ascii", "replace").decode().replace("?", "_")
    return 200, Binary("application/pdf", job.path.read_bytes(), {
        "Content-Disposition": f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name)}",
        "Cache-Control": "no-store",
    })


def undo_set(req: Request) -> "tuple[int, Any]":
    _set_body(req)
    return _set_out(req, _write(req, lambda c: store.undo_last(c, req.params["kind"], req.params["id"])))


def redo_set(req: Request) -> "tuple[int, Any]":
    _set_body(req)
    return _set_out(req, _write(req, lambda c: store.redo_last(c, req.params["kind"], req.params["id"])))


def reset_set(req: Request) -> "tuple[int, Any]":
    _set_body(req)
    return _set_out(req, _write(req, lambda c: store.reset(c, req.params["kind"], req.params["id"])))


def add_portfolio_entry(req: Request) -> "tuple[int, Any]":
    source = _projects(req)
    return _set_out(req, _write(req, lambda c: portfolio_sets.add_entry(c, source, req.params["id"], req.body)))


def set_portfolio_layout(req: Request) -> "tuple[int, Any]":
    body = _set_body(req)
    return _set_out(req, _write(req, lambda c: portfolio_sets.set_layout(c, req.params["id"], req.params["entry"], body.get("plan"),
                                                                         body.get("part", "works"))))


def set_portfolio_texts(req: Request) -> "tuple[int, Any]":
    body = _set_body(req)
    return _set_out(req, _write(req, lambda c: portfolio_sets.set_texts(c, req.params["id"], req.params["entry"], body)))


def delete_portfolio_sheet(req: Request) -> "tuple[int, Any]":
    source = _projects(req)
    try:
        index = int(req.params["index"])
    except ValueError:
        raise ApiError(400, "bad_request", "The page number must be a number.") from None
    return _set_out(req, _write(req, lambda c: portfolio_sets.delete_sheet(c, source, req.params["id"], req.params["entry"],
                                                                           req.params["part"], index)))


def set_portfolio_cover(req: Request) -> "tuple[int, Any]":
    body = _set_body(req)
    return _set_out(req, _write(req, lambda c: portfolio_sets.set_cover_texts(c, req.params["id"], body)))


def edit_portfolio_entry(req: Request) -> "tuple[int, Any]":
    source = _projects(req)
    return _set_out(req, _write(req, lambda c: portfolio_sets.edit_entry(c, source, req.params["id"], req.params["entry"], req.body)))


def delete_portfolio_entry(req: Request) -> "tuple[int, Any]":
    return _set_out(req, _write(req, lambda c: portfolio_sets.delete_entry(c, req.params["id"], req.params["entry"])))


def delete_portfolio_entries(req: Request) -> "tuple[int, Any]":
    ids = _set_body(req).get("entry_ids")
    return _set_out(req, _write(req, lambda c: portfolio_sets.delete_entries(c, req.params["id"], ids)))


def order_portfolio_set(req: Request) -> "tuple[int, Any]":
    ids = _set_body(req).get("entry_ids")
    return _set_out(req, _write(req, lambda c: portfolio_sets.reorder(c, req.params["id"], ids)))


def _ai_inputs(req: Request) -> "tuple[Any, str, Any]":
    """The set to edit, the artist's request and what the model would be shown. Refuses an
    archived set and an empty request before any call is made."""
    body = _set_body(req)
    try:
        prompt = edit_portfolio.clean_prompt(body.get("prompt"))
    except ValueError as e:
        raise ApiError(400, "bad_request", str(e)) from None
    set_id = req.params["id"] if "id" in req.params else body.get("set_id")
    source = _projects(req)

    def read(conn):
        store.require_editable(conn, "portfolio", set_id)
        return store.get(conn, "portfolio", set_id)

    record = _read(req, read)
    try:
        catalog = portfolio_sets.ai_catalog(record, source)
    except ps.SourceError as e:
        raise ApiError(502, "source_error", str(e)) from None
    if any(catalog.project_number(e["project_id"]) is None for e in catalog.entries):
        raise ApiError(409, "conflict", "A project of this set cannot be read right now. Remove it or fix it first.")
    return record, prompt, catalog


def ai_edit_portfolio_set(req: Request) -> "tuple[int, Any]":
    """Edit with AI: one model call, then the whole new content stored as one undo step."""
    client = _client(req)
    record, prompt, catalog = _ai_inputs(req)
    try:
        new = edit_portfolio.edit(client, prompt, catalog, record.id)
    except ClientError as e:
        raise ApiError(502, "llm_error", str(e)) from None
    except edit_portfolio.EditError as e:
        raise ApiError(502, "bad_output", str(e)) from None
    updated = _write(req, lambda c: portfolio_sets.apply_ai_edit(c, record.id, record.content, new))
    status, out = _set_out(req, updated)
    return status, {**out, "ai": {"origin": client.origin, "changed": updated.content != record.content}}


def preview_portfolio_set(req: Request) -> "tuple[int, Any]":
    record = _set_read(req, lambda c: store.get(c, "portfolio", req.params["id"]))
    return 200, _source_call(lambda: portfolio_sets.preview(record, req.ctx.projects, cover=_cover_sheet(req)))


# --- Analyse, Prepare, cost estimate --------------------------------------------
# The model call happens between two short database steps: read and check the
# records, call (no transaction open), then write everything in one transaction.
# So a slow or failing call never holds the database, no partial result is stored
# (spec 4.4.2: on failure the rows stay unchanged), and a record that changed in
# the meantime is refused by the write's own check.

def _client(req: Request) -> Client:
    if req.ctx.client is None:
        raise ApiError(503, "unavailable", "No Claude client is configured.")
    return req.ctx.client


def _read(req: Request, fn: "Callable[[Any], Any]") -> Any:
    """A read with the same error mapping as _write."""
    try:
        with req.ctx.db.read() as conn:
            return fn(conn)
    except errors.NotFound as e:
        raise ApiError(404, "not_found", str(e)) from None
    except errors.Conflict as e:
        raise ApiError(409, "conflict", str(e)) from None
    except ValueError as e:
        raise ApiError(400, "bad_request", str(e)) from None


def _model_error(rec: "opportunity.Opportunity", e: Exception) -> ApiError:
    label = f"{rec.name[:80]}"
    if isinstance(e, ClientError):
        return ApiError(502, "llm_error", f"{label}: {e}")
    return ApiError(502, "bad_output", f"{label}: {e}")


def cost_estimate_endpoint(req: Request) -> "tuple[int, Any]":
    body = _set_body(req)
    action, ids = body.get("action"), body.get("ids")
    if action not in ("analyse", "prepare", "edit_portfolio", "edit_cv"):
        raise ApiError(400, "bad_request", "action must be analyse, prepare, edit_portfolio or edit_cv.")
    client = _client(req)
    if action == "edit_cv":
        _, prompt, catalog = _cv_ai_inputs(req)
        requests = [edit_cv.build_request(prompt, catalog, str(body.get("set_id")))]
        return 200, cost_estimate.estimate(action, requests, mocked=client.origin == "mock")
    if action == "edit_portfolio":
        _, prompt, catalog = _ai_inputs(req)
        requests = [edit_portfolio.build_request(prompt, catalog, str(body.get("set_id")))]
        return 200, cost_estimate.estimate(action, requests, mocked=client.origin == "mock")
    if action == "analyse":
        records = _read(req, lambda c: opportunity.analysable(c, ids))
        requests = [opencall_summarise.build_request(r, req.ctx.profile) for r in records]
    else:
        master = _cv_source(req).get_blocks("full")
        records = _read(req, lambda c: opportunity.preparable(c, ids))
        requests = [adapt_cv.build_request(r, master) for r in records]
    return 200, cost_estimate.estimate(action, requests, mocked=client.origin == "mock")


def analyse_opportunities(req: Request) -> "tuple[int, Any]":
    ids = _body_ids(req)
    client = _client(req)
    records = _read(req, lambda c: opportunity.analysable(c, ids))
    results = []
    for rec in records:
        try:
            results.append((rec, opencall_summarise.analyse(client, rec, req.ctx.profile)))
        except (ClientError, opencall_summarise.AnalysisError) as e:
            raise _model_error(rec, e) from None
    return _items(_write(req, lambda c: [
        opportunity.record_analysis(c, rec.id, dataclasses.asdict(a), client.origin) for rec, a in results
    ]))


def prepare_opportunities(req: Request) -> "tuple[int, Any]":
    ids = _body_ids(req)
    client, source = _client(req), _cv_source(req)
    records = _read(req, lambda c: opportunity.preparable(c, ids))
    master = _source_call(lambda: source.get_blocks("full"))
    chosen = []
    for rec in records:
        try:
            chosen.append((rec, adapt_cv.adapt(client, rec, master)))
        except (ClientError, adapt_cv.AdaptError) as e:
            raise _model_error(rec, e) from None
    now = _now()
    made = _write(req, lambda c: [(rec.id, *prepare.create_sets(c, rec, blocks, now)) for rec, blocks in chosen])
    return 200, {"items": [
        {"opportunity": opportunity.get(req.ctx.db, oid).to_api(), "cv_set_id": cv_id, "portfolio_set_id": pf_id}
        for oid, cv_id, pf_id in made
    ]}


# --- Applications: Mark Applied, status, replies (shown on For You) ---------------------

def apply_opportunities(req: Request) -> "tuple[int, Any]":
    ids = _body_ids(req)
    return _items(_write(req, lambda c: opportunity.apply(c, ids, _now())))


def set_application_status(req: Request) -> "tuple[int, Any]":
    status = _set_body(req).get("status")
    return 200, _write(req, lambda c: opportunity.set_status(c, req.params["id"], status, _now())).to_api()


def intake_run(req: Request) -> "tuple[int, Any]":
    """Load opportunities: read every connected source and keep what is new. No Claude
    call, so no cost popup."""
    _set_body(req)
    if not req.ctx.intake:
        raise ApiError(503, "unavailable", "No intake source is configured (intake.sources).")
    try:
        applied = opportunity.list_all(req.ctx.db, applied=True)
        return 200, intake.run(req.ctx.db, req.ctx.intake, _now(), req.ctx.intake_filters, applied)
    except IntakeError as e:
        raise ApiError(502, "source_error", str(e)) from None


def tracker_refresh(req: Request) -> "tuple[int, Any]":
    """Read the mailbox and keep what is new. No Claude call, so no cost popup."""
    _set_body(req)
    if req.ctx.mail is None:
        raise ApiError(503, "unavailable", "No mail source is configured.")
    applied = opportunity.list_all(req.ctx.db, applied=True)      # archived rows included (spec 7.4)
    try:
        incoming = req.ctx.mail.fetch(applied)
    except MailError as e:
        raise ApiError(502, "source_error", str(e)) from None
    return 200, _write(req, lambda c: update.refresh(c, incoming))


def list_updates(req: Request) -> "tuple[int, Any]":
    return 200, {"items": _set_read(req, lambda c: update.list_for(c, req.params["id"]))}


def read_updates(req: Request) -> "tuple[int, Any]":
    _set_body(req)
    _write(req, lambda c: update.mark_read(c, req.params["id"]))
    record = opportunity.get(req.ctx.db, req.params["id"])
    return 200, {"opportunity": record.to_api(), "unread": _unread(req)}


# --- CV: referents and blocks -------------------------------------------------

def _cv_source(req: Request) -> CvSource:
    if req.ctx.cv is None:
        raise ApiError(503, "unavailable", "No CV source is configured.")
    return req.ctx.cv


def list_cv_referents(req: Request) -> "tuple[int, Any]":
    source = _cv_source(req)
    return 200, {"items": _set_read(req, lambda c: cv_sets.list_referents(c, source))}


def get_cv_referent_blocks(req: Request) -> "tuple[int, Any]":
    source = _cv_source(req)
    return 200, {"items": _source_call(lambda: _set_read(req, lambda c: cv_sets.referent_blocks(c, source, req.params["id"])))}


def add_cv_block(req: Request) -> "tuple[int, Any]":
    source = _cv_source(req)
    return _set_out(req, _write(req, lambda c: cv_sets.add_block(c, source, req.params["id"], req.body)))


def add_all_cv_blocks(req: Request) -> "tuple[int, Any]":
    source = _cv_source(req)
    return _set_out(req, _write(req, lambda c: cv_sets.add_all(c, source, req.params["id"], req.body)))


def remove_all_cv_blocks(req: Request) -> "tuple[int, Any]":
    return _set_out(req, _write(req, lambda c: cv_sets.remove_all(c, req.params["id"])))


def edit_cv_block(req: Request) -> "tuple[int, Any]":
    text = _set_body(req).get("text")
    return _set_out(req, _write(req, lambda c: cv_sets.edit_block(c, req.params["id"], req.params["block"], text)))


def delete_cv_block(req: Request) -> "tuple[int, Any]":
    return _set_out(req, _write(req, lambda c: cv_sets.delete_block(c, req.params["id"], req.params["block"])))


def order_cv_set(req: Request) -> "tuple[int, Any]":
    ids = _set_body(req).get("block_ids")
    return _set_out(req, _write(req, lambda c: cv_sets.reorder(c, req.params["id"], ids)))


def _cv_ai_inputs(req: Request) -> "tuple[Any, str, Any]":
    """The CV set to edit, the artist's request and what the model would be shown. Refuses an
    archived set and an empty request before any call is made."""
    body = _set_body(req)
    try:
        prompt = edit_cv.clean_prompt(body.get("prompt"))
    except ValueError as e:
        raise ApiError(400, "bad_request", str(e)) from None
    set_id = req.params["id"] if "id" in req.params else body.get("set_id")
    source = _cv_source(req)

    def read(conn):
        store.require_editable(conn, "cv", set_id)
        return store.get(conn, "cv", set_id)

    record = _read(req, read)
    return record, prompt, _source_call(lambda: cv_sets.ai_catalog(record, source))


def ai_edit_cv_set(req: Request) -> "tuple[int, Any]":
    """Edit with AI: one model call, then the whole new content stored as one undo step."""
    client = _client(req)
    record, prompt, catalog = _cv_ai_inputs(req)
    try:
        new = edit_cv.edit(client, prompt, catalog, record.id)
    except ClientError as e:
        raise ApiError(502, "llm_error", str(e)) from None
    except edit_cv.EditError as e:
        raise ApiError(502, "bad_output", str(e)) from None
    updated = _write(req, lambda c: cv_sets.apply_ai_edit(c, record.id, record.content, new))
    status, out = _set_out(req, updated)
    return status, {**out, "ai": {"origin": client.origin, "changed": updated.content != record.content}}


def preview_cv_set(req: Request) -> "tuple[int, Any]":
    return 200, cv_sets.preview(_set_read(req, lambda c: store.get(c, "cv", req.params["id"])))


# Handlers that read mailboxes, parse pages or call Claude: with a Jobs they run in its
# worker process (web/jobs.py), so the rest of the API never waits behind them.
HEAVY = frozenset({intake_run, tracker_refresh, analyse_opportunities, prepare_opportunities,
                   ai_edit_portfolio_set, ai_edit_cv_set})


def build_router() -> Router:
    r = Router()
    r.add("GET", "/api/health", health)
    r.add("GET", "/api/opportunities", list_opportunities)
    r.add("GET", "/api/opportunities/{id}", get_opportunity)
    r.add("PATCH", "/api/opportunities/{id}", patch_opportunity)
    r.add("POST", "/api/opportunities/archive", archive_opportunities)
    r.add("POST", "/api/opportunities/unarchive", unarchive_opportunities)
    r.add("POST", "/api/opportunities/promote", promote_opportunities)
    r.add("POST", "/api/opportunities/saved", save_opportunities)
    r.add("POST", "/api/opportunities/apply", apply_opportunities)
    r.add("POST", "/api/opportunities/analyse", analyse_opportunities)
    r.add("POST", "/api/opportunities/prepare", prepare_opportunities)
    r.add("POST", "/api/cost-estimate", cost_estimate_endpoint)
    r.add("PUT", "/api/opportunities/{id}/application", set_application_status)
    r.add("GET", "/api/opportunities/{id}/updates", list_updates)
    r.add("POST", "/api/opportunities/{id}/updates/read", read_updates)
    r.add("POST", "/api/tracker/refresh", tracker_refresh)
    r.add("POST", "/api/intake/run", intake_run)
    r.add("GET", "/api/projects", list_projects)
    r.add("POST", "/api/projects/refresh", refresh_projects)
    r.add("GET", "/api/projects/{id}", get_project)
    r.add("GET", "/api/thumbs/{project_id}/{image_id}", get_thumb)
    r.add("GET", "/api/portfolio/cover", get_cover)
    r.add("GET", "/api/portfolio/cover/photo", get_cover_photo)
    sets = "/api/sets/{kind}"     # both kinds: portfolio, cv
    r.add("GET", sets, list_set)
    r.add("POST", sets, create_set)
    r.add("PUT", sets + "/order", order_sets)
    r.add("GET", sets + "/{id}", get_set)
    r.add("PATCH", sets + "/{id}", patch_set)
    r.add("DELETE", sets + "/{id}", delete_set)
    r.add("POST", sets + "/{id}/save", save_set)
    r.add("POST", sets + "/{id}/duplicate", duplicate_set)
    r.add("POST", sets + "/{id}/archive", archive_set)
    r.add("POST", sets + "/{id}/unarchive", unarchive_set)
    r.add("POST", sets + "/{id}/undo", undo_set)
    r.add("POST", sets + "/{id}/redo", redo_set)
    r.add("POST", sets + "/{id}/reset", reset_set)
    r.add("POST", sets + "/{id}/pdf", start_pdf)
    r.add("GET", "/api/exports", list_exports)
    r.add("GET", "/api/exports/{job}", pdf_state)
    r.add("DELETE", "/api/exports/{job}", stop_pdf)
    r.add("GET", "/api/exports/{job}/file", pdf_file)
    base = "/api/sets/portfolio"
    r.add("POST", base + "/full", create_full_portfolio_set)
    r.add("POST", base + "/{id}/entries", add_portfolio_entry)
    r.add("POST", base + "/{id}/entries/delete", delete_portfolio_entries)
    r.add("PUT", base + "/{id}/entries/{entry}", edit_portfolio_entry)
    r.add("DELETE", base + "/{id}/entries/{entry}", delete_portfolio_entry)
    r.add("PUT", base + "/{id}/entries/{entry}/layout", set_portfolio_layout)
    r.add("PUT", base + "/{id}/entries/{entry}/texts", set_portfolio_texts)
    r.add("PUT", base + "/{id}/cover", set_portfolio_cover)
    r.add("DELETE", base + "/{id}/entries/{entry}/sheets/{part}/{index}", delete_portfolio_sheet)
    r.add("PUT", base + "/{id}/order", order_portfolio_set)
    r.add("GET", base + "/{id}/preview", preview_portfolio_set)
    r.add("POST", base + "/{id}/ai-edit", ai_edit_portfolio_set)
    r.add("GET", "/api/cv-referents", list_cv_referents)
    r.add("GET", "/api/cv-referents/{id}/blocks", get_cv_referent_blocks)
    base = "/api/sets/cv"
    r.add("POST", base + "/{id}/blocks", add_cv_block)
    r.add("POST", base + "/{id}/blocks/all", add_all_cv_blocks)
    r.add("DELETE", base + "/{id}/blocks", remove_all_cv_blocks)
    r.add("PUT", base + "/{id}/blocks/{block}", edit_cv_block)
    r.add("DELETE", base + "/{id}/blocks/{block}", delete_cv_block)
    r.add("PUT", base + "/{id}/order", order_cv_set)
    r.add("GET", base + "/{id}/preview", preview_cv_set)
    r.add("POST", base + "/{id}/ai-edit", ai_edit_cv_set)
    return r


# --- http plumbing ----------------------------------------------------------

def make_handler(ctx: Context, router: Router) -> type[BaseHTTPRequestHandler]:
    class ApiHandler(BaseHTTPRequestHandler):
        server_version = "arta"
        sys_version = ""

        def _send(self, status: int, payload: Any, headers: "dict[str, str] | None" = None) -> None:
            if isinstance(payload, Binary):
                data, ctype, extra = payload.data, payload.content_type, payload.headers
            else:
                data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                ctype, extra = "application/json; charset=utf-8", {"Cache-Control": "no-store"}
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            for k, v in {**static.SECURITY_HEADERS, **extra, **(headers or {})}.items():
                self.send_header(k, v)
            self.end_headers()
            if data:
                self.wfile.write(data)

        def _discard(self, length: int) -> None:
            """Read and drop an oversized body before answering 413. Replying while
            the client is still sending makes it see a reset instead of the 413.
            Absurd lengths are not drained: the connection is closed instead."""
            if 0 < length <= 8 * MAX_BODY:
                remaining = length
                while remaining:
                    chunk = self.rfile.read(min(65536, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
            else:
                self.close_connection = True

        def _read_body(self) -> Any:
            ctype = self.headers.get("Content-Type", "").split(";")[0].strip().lower()
            if ctype != "application/json":
                raise ApiError(415, "unsupported_media_type", "Writes must be application/json.")
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                raise ApiError(411, "length_required", "Content-Length is required.") from None
            if length < 0 or length > MAX_BODY:
                self._discard(length)
                raise ApiError(413, "too_large", f"Body must be at most {MAX_BODY} bytes.")
            raw = self.rfile.read(length)
            try:
                return json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise ApiError(400, "bad_json", "Body is not valid JSON.") from None

        def _origins(self) -> "tuple[set[str], set[str]]":
            """The Host values and Origins of this server, on the port it actually listens on."""
            port = self.server.server_address[1]
            hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
            return hosts, {f"http://{h}" for h in hosts}

        def _refused(self, method: str, path: str) -> bool:
            """Host and Origin checks; on refusal the 403 is sent here."""
            hosts, origins = self._origins()
            host = self.headers.get("Host", "").strip().lower()
            if host not in hosts:
                reason, code = f"Host {host!r}", "bad_host"
            elif method not in ("GET", "HEAD") and self.headers.get("Origin", "").strip().lower() not in origins:
                reason, code = f"Origin {self.headers.get('Origin', '')!r}", "bad_origin"
            else:
                return False
            self.close_connection = True   # an unread body is not drained
            self._send(403, {"error": {"code": code, "message": "Forbidden."}})
            log.warning("%s %s -> 403 (%s)", method, path, reason)
            return True

        def _static(self, method: str, path: str) -> int:
            if method != "GET":
                self._send(405, {"error": {"code": "method_not_allowed", "message": f"{method} is not allowed on {path}."}})
                return 405
            target = static.resolve(REPO_ROOT, unquote(path))
            if target is None:
                self._send(404, {"error": {"code": "not_found", "message": f"No file {path}."}})
                return 404
            self._send(200, Binary(static.content_type(target), target.read_bytes(), {"Cache-Control": "no-cache"}))
            return 200

        def _dispatch(self, method: str) -> None:
            parts = urlsplit(self.path)
            status = 500
            # Before routing and before the body is read: a refused caller learns nothing
            # about which routes or files exist.
            if self._refused(method, parts.path):
                return
            if not parts.path.startswith("/api/"):
                status = self._static(method, parts.path)
                if status != 200:
                    log.info("%s %s -> %s", method, parts.path, status)
                return
            try:
                handler, params = router.match(method, parts.path)
                # POST/PUT/PATCH carry a required JSON body; GET/DELETE carry none.
                body = self._read_body() if method in ("POST", "PUT", "PATCH") else None
                query = parse_qs(parts.query)
                if ctx.jobs is not None and handler in HEAVY:
                    status, payload = ctx.jobs.run(handler, method, parts.path, params, query, body)
                else:
                    status, payload = handler(Request(ctx, method, parts.path, params, query, body, self.headers))
            except ApiError as e:
                status, payload = e.status, {"error": {"code": e.code, "message": e.message}}
            except Exception:
                log.exception("unhandled error: %s %s", method, parts.path)
                status = 500
                payload = {"error": {"code": "internal", "message": "Internal error."}}
            self._send(status, payload)
            log.info("%s %s -> %s", method, parts.path, status)

        def do_GET(self) -> None: self._dispatch("GET")
        def do_POST(self) -> None: self._dispatch("POST")
        def do_PUT(self) -> None: self._dispatch("PUT")
        def do_PATCH(self) -> None: self._dispatch("PATCH")
        def do_DELETE(self) -> None: self._dispatch("DELETE")

        def log_message(self, *args: Any) -> None:  # we log ourselves, without bodies
            pass

    return ApiHandler


def _google(cfg: Config, api: str) -> "Callable[[], Any]":
    """A factory for the dedicated account's read-only Gmail or Drive client, from
    google.token_file (config.parse refuses a Google source without it)."""
    def factory() -> Any:
        from core import google_auth  # imported here: the Google libraries are optional elsewhere
        if cfg.google_token_file is None:
            raise google_auth.GoogleAuthError("google.token_file is not set in the configuration.")
        make = google_auth.gmail_service if api == "gmail" else google_auth.drive_service
        return make(cfg.google_token_file)
    return factory


def build_mail(cfg: Config) -> "MailUpdates | None":
    if cfg.updates == "gmail":
        return GmailMailUpdates(_google(cfg, "gmail"))
    if cfg.updates == "fixtures":
        return FixtureMailUpdates(cfg.fixtures_dir / "updates.json")
    if cfg.updates == "demo":
        return FixtureMailUpdates(cfg.demo_dir / "updates.json")
    return None


def build_intake(cfg: Config) -> "list[IntakeSource] | None":
    if not cfg.intake:
        return None
    sources: "list[IntakeSource]" = []
    for name in cfg.intake:
        if name == "fixtures":
            sources.append(FixtureIntake(cfg.fixtures_dir / "emails.json"))
        elif name == "demo":
            sources.append(FixtureIntake(cfg.demo_dir / "emails.json"))
        elif name == "gmail":
            sources.append(GmailIntake(_google(cfg, "gmail")))
        elif name == "artconnect":
            sources.append(ArtConnectSource(debug_dir=cfg.data_dir / "drift"))
    return sources


def build_cover(cfg: Config) -> "DriveCover | FileCover | None":
    if cfg.cover_drive_file:
        return DriveCover(cfg.cover_drive_file, cfg.cover_cache_dir, _google(cfg, "drive"))
    if cfg.cover_file:
        return FileCover(cfg.cover_file, cfg.cover_cache_dir)
    return None


def build_cv(cfg: Config) -> "CvSource | None":
    if cfg.cv == "drive" and cfg.cv_drive_file:
        return DriveCv(cfg.cv_drive_file, cfg.cv_cache_path, _google(cfg, "drive"))
    if cfg.cv == "local" and cfg.cv_local_file:
        return LocalCv(cfg.cv_local_file)
    if cfg.cv == "fixtures":
        return LocalCv(cfg.fixture_cv_file)
    if cfg.cv == "demo":
        return LocalCv(cfg.demo_dir / "cv" / "master.json")
    return None


def build_projects(cfg: Config) -> "ps.ProjectSource | None":
    if cfg.projects is None:
        return None
    registry = load_registry(cfg.registry_file) if cfg.registry_file else None
    if cfg.projects == "drive" and cfg.drive_folder:
        return DriveProjects(cfg.drive_projects_dir, registry, _google(cfg, "drive"),
                             folder=cfg.drive_folder, categories=cfg.drive_categories)
    if cfg.projects == "local" and cfg.projects_local_dir:
        return LocalProjects(cfg.projects_local_dir, registry)
    if cfg.projects == "fixtures":
        return LocalProjects(cfg.fixture_projects_dir, registry)
    if cfg.projects == "demo":
        return LocalProjects(cfg.demo_projects_dir, registry)
    return None


def build_context(
    cfg: Config,
    db: Database,
    projects: "ps.ProjectSource | None" = None,
    cv: "CvSource | None" = None,
    mail: "MailUpdates | None" = None,
    client: "Client | None" = None,
    intake_sources: "list[IntakeSource] | None" = None,
    intake_filters: "IntakeFilters | None" = None,
    profile: str | None = None,
    cover: "DriveCover | FileCover | None" = None,
) -> Context:
    """The handlers' Context: what is passed in, the rest built from cfg. The API process
    and its worker (web/jobs.py) both build theirs here."""
    return Context(
        cfg, db, projects or build_projects(cfg), ThumbCache(cfg.thumb_dir),
        cv or build_cv(cfg), mail or build_mail(cfg), client or make_client(cfg),
        intake_sources if intake_sources is not None else build_intake(cfg),
        intake_filters, profile, cover=cover or build_cover(cfg),
    )


def load_runtime(cfg: Config) -> "dict[str, Any]":
    """The Claude client, the artist profile and the intake filters for cfg; raises what
    main() reports as "cannot start"."""
    return {
        "client": make_client(cfg),
        "profile": artist_profile.load(cfg.profile_file) if cfg.profile_file else None,
        "intake_filters": load_intake_filters(cfg.intake_filters_path) if cfg.intake else None,
    }


def make_server(
    cfg: Config,
    db: Database,
    port: int | None = None,
    router: Router | None = None,
    projects: "ps.ProjectSource | None" = None,
    cv: "CvSource | None" = None,
    mail: "MailUpdates | None" = None,
    client: "Client | None" = None,
    intake_sources: "list[IntakeSource] | None" = None,
    intake_filters: "IntakeFilters | None" = None,
    profile: str | None = None,
    cover: "DriveCover | FileCover | None" = None,
    jobs: "Jobs | None" = None,
    exports: "Exports | None" = None,
) -> ThreadingHTTPServer:
    ctx = build_context(cfg, db, projects=projects, cv=cv, mail=mail, client=client, intake_sources=intake_sources,
                        intake_filters=intake_filters, profile=profile, cover=cover)
    ctx.jobs, ctx.exports = jobs, exports
    handler = make_handler(ctx, router or build_router())
    server = ThreadingHTTPServer((HOST, cfg.port if port is None else port), handler)
    server.daemon_threads = True
    return server


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        cfg = config_mod.load()
    except ConfigError as e:
        print(f"arta: cannot start: {e}", file=sys.stderr)
        return 2
    return serve(cfg)


def serve(cfg: Config) -> int:
    """Run the dashboard for cfg until it is stopped (main() and `python -m demo`)."""
    try:
        db = Database(cfg.db_path)
        version = db.migrate()
        runtime = load_runtime(cfg)
    except (ConfigError, DbError, ClientError, FilterConfigError, artist_profile.ProfileError, ValueError) as e:
        print(f"arta: cannot start: {e}", file=sys.stderr)
        return 2

    jobs, export_jobs = Jobs(cfg), Jobs(cfg)
    exports = Exports(cfg.exports_dir, lambda kind, set_id, path: export_jobs.submit("render_pdf", kind, set_id, path))
    server = make_server(cfg, db, jobs=jobs, exports=exports, **runtime)
    log.info(
        "listening on http://%s:%s/ sources=%s llm=%s schema=%s profile_chars=%d",
        HOST, cfg.port, _sources(cfg), cfg.llm, version, len(runtime["profile"] or ""),
    )
    # launchctl stop sends SIGTERM; shutdown() must run off the serving thread.
    signal.signal(
        signal.SIGTERM, lambda *_: threading.Thread(target=server.shutdown, daemon=True).start()
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        jobs.close()
        export_jobs.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
