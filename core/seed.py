"""Fixture loader: seeds the database from tests/fixtures/.

Test helpers only. The server does not seed at startup (dsgnrfhmn, 2026-10-10: the
dashboard works with real intake); each loader fills an empty table and
returns 0 otherwise.

The set covers every For You UI state (For Review, Analysed, prepared,
applied, Archived, Filtered, empty description, saved, notes). It was lifted
from the static for-you.html rows so the page rendered from the API matches
the pre-API page. Analysed rows are labelled analysis_origin "mock".
"""

from __future__ import annotations

import json
import secrets
from pathlib import Path

from core.db import Database
from models import opportunity, update
from layout import sheets
from sets import store
from sources.mail_updates import IncomingUpdate


def seed_opportunities(db: Database, fixtures_dir: Path) -> int:
    """Insert fixture opportunities if the table is empty. Returns rows inserted."""
    path = fixtures_dir / "opportunities.json"
    records = [opportunity.Opportunity.from_seed(d) for d in json.loads(path.read_text("utf-8"))]
    with db.tx() as conn:
        if conn.execute("SELECT 1 FROM opportunities LIMIT 1").fetchone():
            return 0
        for r in records:
            opportunity.insert(conn, r)
    return len(records)


def seed_portfolio_sets(db: Database, fixtures_dir: Path, source) -> int:
    """Portfolio sets over the fixture project, only into an empty table.

    Entries name a project and pick images by position (works and exhibitions as
    classified by sources/image_meta), so the fixture does not hard-code
    path hashes. Anything that does not resolve raises: a fixture that no longer
    matches the tree must be noticed, not skipped."""
    import secrets

    specs = json.loads((fixtures_dir / "portfolio_sets.json").read_text("utf-8"))
    projects = {p.name: p for p in source.list_projects()}
    with db.tx() as conn:
        if conn.execute("SELECT 1 FROM sets WHERE kind = 'portfolio' LIMIT 1").fetchone():
            return 0
        for spec in specs:
            content = []
            for e in spec["entries"]:
                project = projects[e["project"]]
                works = [i.id for i in project.images if i.role == "work"]
                shows = [i.id for i in project.images if i.role == "exhibition"]
                entry = {
                    "id": secrets.token_hex(8),
                    "project_id": project.id,
                    "works": [works[i] for i in e["works"]],
                    "exhibitions": [shows[i] for i in e["exhibitions"]],
                    "text_amount": e["text_amount"],
                }
                if "layout_plan" in e:       # the set's own page layouts, as the layout menu stores them
                    plan = e["layout_plan"]
                    if not plan or plan[0] not in sheets.OPENING_LAYOUTS or any(x not in sheets.WORK_LAYOUTS for x in plan[1:]):
                        raise ValueError(f"Fixture layout_plan {plan!r} of {spec['name']!r} names an unknown layout.")
                    entry["layout_plan"] = list(plan)
                content.append(entry)
            opportunity_id = None
            if "opportunity" in spec:
                rows = conn.execute("SELECT id FROM opportunities WHERE name = ?", (spec["opportunity"],)).fetchall()
                if len(rows) != 1:
                    raise ValueError(f"Fixture opportunity {spec['opportunity']!r} matches {len(rows)} records, expected 1.")
                opportunity_id = rows[0]["id"]
            saved_at, archived_at = spec.get("saved_at"), spec.get("archived_at")
            conn.execute(
                "INSERT INTO sets (id, kind, name, saved, saved_at, archived, archived_at, opportunity_id,"
                " content, baseline, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (secrets.token_hex(8), "portfolio", spec["name"], int(bool(saved_at)), saved_at,
                 int(bool(archived_at)), archived_at, opportunity_id, json.dumps(content),
                 json.dumps(content) if saved_at else "[]", spec["created_at"]),
            )
        store.settle_positions(conn, "portfolio")
    return len(specs)


def seed_cv_sets(db: Database, fixtures_dir: Path, source) -> int:
    """CV sets over the fixture master blocks, only while there is no CV set.

    A spec names blocks by id. Every id must resolve in the Full CV referent;
    anything that does not raises: a fixture that no longer matches the master
    must be noticed, not skipped."""
    specs = json.loads((fixtures_dir / "cv" / "sets.json").read_text("utf-8"))
    master = {b.id: b for b in source.get_blocks("full")}
    with db.tx() as conn:
        if conn.execute("SELECT 1 FROM sets WHERE kind = 'cv' LIMIT 1").fetchone():
            return 0
        for spec in specs:
            missing = [i for i in spec["blocks"] if i not in master]
            if missing:
                raise ValueError(f"Fixture CV set {spec['name']!r} names unknown block {missing[0]}.")
            content = [master[i].to_api() for i in spec["blocks"]]
            saved_at = spec.get("saved_at")
            conn.execute(
                "INSERT INTO sets (id, kind, name, saved, saved_at, archived, archived_at, opportunity_id,"
                " content, baseline, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (secrets.token_hex(8), "cv", spec["name"], int(bool(saved_at)), saved_at, 0, None, None,
                 json.dumps(content), json.dumps(content) if saved_at else "[]", spec["created_at"]),
            )
        store.settle_positions(conn, "cv")
    return len(specs)


def seed_updates(db: Database, fixtures_dir: Path) -> int:
    """Replies already received when the database is first made: the fixture
    messages marked "seed", and only while there is no update at all. The rest
    stay in the mailbox for Tracker Refresh to pick up, so Refresh has something
    to find. A message that names an opportunity that does not exist raises."""
    messages = json.loads((fixtures_dir / "updates.json").read_text("utf-8"))
    with db.tx() as conn:
        known = {i for (i,) in conn.execute("SELECT id FROM opportunities")}
        unknown = [m["opportunity_id"] for m in messages if m["opportunity_id"] not in known]
        if unknown:
            raise ValueError(f"Fixture mail names an unknown opportunity: {unknown[0]!r}.")
        if conn.execute("SELECT 1 FROM updates LIMIT 1").fetchone():
            return 0
        seeded = [
            IncomingUpdate(m["message_id"], m["opportunity_id"], m["sender"], m["received_at"], m["subject"], m["body"])
            for m in messages if m["seed"]
        ]
        return update.store(conn, seeded)
