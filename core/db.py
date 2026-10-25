"""SQLite access: one connection policy, numbered migrations, transactions.

Policy:
- No module opens the database file on its own; everything goes through
  Database.
- A connection lives for one read() or one tx(). SQLite connections are
  cheap and this keeps threads from ever sharing one.
- Every mutation runs inside tx(): BEGIN IMMEDIATE ... COMMIT, rolled back
  on any exception. Concurrent writers (dashboard click, intake, Refresh)
  serialize on the write lock instead of losing an update.
- The file and its directory are created owner-only (0600 / 0700).

Schema changes are new entries appended to MIGRATIONS. Never edit an
applied entry. The applied version is stored in PRAGMA user_version.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


class DbError(Exception):
    pass


# (version, name, sql). Versions are consecutive from 1.
MIGRATIONS: list[tuple[int, str, str]] = [
    (
        1,
        "opportunities and intake_seen",
        """
        -- One record per opportunity, independent fields.
        -- The Tracker is a filter: application_status IS NOT NULL.
        CREATE TABLE opportunities (
            id                 TEXT PRIMARY KEY,
            stage              TEXT NOT NULL
                               CHECK (stage IN ('filtered', 'for_review', 'analysed')),
            prepared_cv_set_id TEXT,
            application_status TEXT
                               CHECK (application_status IN ('applied', 'approved', 'declined')),
            applied_at         TEXT,
            archived           INTEGER NOT NULL DEFAULT 0 CHECK (archived IN (0, 1)),
            archived_at        TEXT,
            saved              INTEGER NOT NULL DEFAULT 0 CHECK (saved IN (0, 1)),
            name               TEXT NOT NULL,
            type               TEXT NOT NULL CHECK (type IN ('open_call', 'grant')),
            source             TEXT NOT NULL,
            link               TEXT,
            description        TEXT NOT NULL DEFAULT '',
            summary            TEXT,
            eligible           TEXT,
            relevance          REAL,
            deadline           TEXT,
            residency_start    TEXT,
            residency_end      TEXT,
            decision_date      TEXT,
            notes              TEXT NOT NULL DEFAULT '',
            analysis_origin    TEXT CHECK (analysis_origin IN ('mock', 'live')),
            created_at         TEXT NOT NULL,
            CHECK ((archived = 1) = (archived_at IS NOT NULL)),
            CHECK ((application_status IS NULL) = (applied_at IS NULL)),
            -- Analysis fields exist only where analysis ran.
            CHECK (stage = 'analysed' OR analysis_origin IS NULL)
        );
        CREATE INDEX opportunities_stage ON opportunities (stage, archived);
        CREATE INDEX opportunities_application ON opportunities (application_status);

        -- Message identifiers already processed by intake. Deduplication lives
        -- here, not in the IMAP \\Seen flag (read-only scopes).
        CREATE TABLE intake_seen (
            message_id TEXT PRIMARY KEY,
            seen_at    TEXT NOT NULL
        );
        """,
    ),
    (
        2,
        "tracker archive flag on application",
        """
        -- The Tracker's Archived tab is independent of For You's Archived table
        -- (spec 4.5 vs 7.7). Archiving an applied row in one place must not
        -- move it in the other, so the Tracker gets its own flag, which
        -- belongs to the application and exists only while one does.
        ALTER TABLE opportunities
            ADD COLUMN application_archived INTEGER NOT NULL DEFAULT 0
            CHECK (application_archived IN (0, 1));
        ALTER TABLE opportunities
            ADD COLUMN application_archived_at TEXT
            CHECK (
                (application_archived_at IS NOT NULL) = (application_archived = 1)
                AND (application_archived = 0 OR application_status IS NOT NULL)
            );
        """,
    ),
    (
        3,
        "filter reason",
        """
        -- Why intake put a record into Filtered (the "Reason" column). It is
        -- not a summary: no analysis ran. It exists only while the record is
        -- filtered; promoting or archiving code must clear it, and the CHECK
        -- makes forgetting fail loud.
        ALTER TABLE opportunities
            ADD COLUMN filter_reason TEXT
            CHECK (filter_reason IS NULL OR stage = 'filtered');
        """,
    ),
    (
        4,
        "sets and undo",
        """
        -- Portfolio and CV sets share one table: saving, archiving, deleting
        -- and undo are one implementation. `content`
        -- is the kind's own JSON (portfolio: project entries; cv: blocks).
        -- `baseline` is the content at the last save, which Reset returns to.
        CREATE TABLE sets (
            id             TEXT PRIMARY KEY,
            kind           TEXT NOT NULL CHECK (kind IN ('portfolio', 'cv')),
            name           TEXT NOT NULL CHECK (length(name) > 0),
            saved          INTEGER NOT NULL DEFAULT 0 CHECK (saved IN (0, 1)),
            saved_at       TEXT,
            archived       INTEGER NOT NULL DEFAULT 0 CHECK (archived IN (0, 1)),
            archived_at    TEXT,
            opportunity_id TEXT REFERENCES opportunities (id) ON DELETE SET NULL,
            content        TEXT NOT NULL DEFAULT '[]',
            baseline       TEXT NOT NULL DEFAULT '[]',
            created_at     TEXT NOT NULL,
            CHECK ((saved = 1) = (saved_at IS NOT NULL)),
            CHECK ((archived = 1) = (archived_at IS NOT NULL))
        );
        CREATE INDEX sets_kind ON sets (kind, archived);

        -- One row per undo step: the content before the change. The newest
        -- 20 per set are kept (sets/undo.py).
        CREATE TABLE set_undo (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            set_id   TEXT NOT NULL REFERENCES sets (id) ON DELETE CASCADE,
            snapshot TEXT NOT NULL
        );
        CREATE INDEX set_undo_set ON set_undo (set_id, id);
        """,
    ),
    (
        5,
        "tracker updates",
        """
        -- Replies matched to an applied opportunity (Tracker, spec 7.5). The
        -- mail source only reads; what has been seen is kept here, so a message
        -- is stored once (message_id UNIQUE is the seen-set) and marking it read
        -- never touches the mailbox. Bodies are sanitized before they are stored.
        CREATE TABLE updates (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id     TEXT NOT NULL UNIQUE,
            opportunity_id TEXT NOT NULL REFERENCES opportunities (id) ON DELETE CASCADE,
            sender         TEXT NOT NULL,
            received_at    TEXT NOT NULL,
            subject        TEXT NOT NULL DEFAULT '',
            body           TEXT NOT NULL,
            read           INTEGER NOT NULL DEFAULT 0 CHECK (read IN (0, 1))
        );
        CREATE INDEX updates_opportunity ON updates (opportunity_id, read);
        """,
    ),
    (
        6,
        "application status pending",
        """
        -- Adds 'pending' to the application statuses. SQLite cannot alter a CHECK,
        -- so the table is rebuilt (its documented 12-step procedure). This
        -- version is in REBUILD_VERSIONS: it runs with foreign keys off, because
        -- dropping opportunities would otherwise cascade into updates and null
        -- sets.opportunity_id. foreign_key_check runs before the commit.
        CREATE TABLE opportunities_new (
            id                 TEXT PRIMARY KEY,
            stage              TEXT NOT NULL
                               CHECK (stage IN ('filtered', 'for_review', 'analysed')),
            prepared_cv_set_id TEXT,
            application_status TEXT
                               CHECK (application_status IN ('applied', 'approved', 'pending', 'declined')),
            applied_at         TEXT,
            archived           INTEGER NOT NULL DEFAULT 0 CHECK (archived IN (0, 1)),
            archived_at        TEXT,
            saved              INTEGER NOT NULL DEFAULT 0 CHECK (saved IN (0, 1)),
            name               TEXT NOT NULL,
            type               TEXT NOT NULL CHECK (type IN ('open_call', 'grant')),
            source             TEXT NOT NULL,
            link               TEXT,
            description        TEXT NOT NULL DEFAULT '',
            summary            TEXT,
            eligible           TEXT,
            relevance          REAL,
            deadline           TEXT,
            residency_start    TEXT,
            residency_end      TEXT,
            decision_date      TEXT,
            notes              TEXT NOT NULL DEFAULT '',
            analysis_origin    TEXT CHECK (analysis_origin IN ('mock', 'live')),
            created_at         TEXT NOT NULL,
            application_archived INTEGER NOT NULL DEFAULT 0
                               CHECK (application_archived IN (0, 1)),
            application_archived_at TEXT
                               CHECK (
                                   (application_archived_at IS NOT NULL) = (application_archived = 1)
                                   AND (application_archived = 0 OR application_status IS NOT NULL)
                               ),
            filter_reason      TEXT
                               CHECK (filter_reason IS NULL OR stage = 'filtered'),
            CHECK ((archived = 1) = (archived_at IS NOT NULL)),
            CHECK ((application_status IS NULL) = (applied_at IS NULL)),
            CHECK (stage = 'analysed' OR analysis_origin IS NULL)
        );
        INSERT INTO opportunities_new (
            id, stage, prepared_cv_set_id, application_status, applied_at,
            archived, archived_at, saved, name, type, source, link, description,
            summary, eligible, relevance, deadline, residency_start, residency_end,
            decision_date, notes, analysis_origin, created_at,
            application_archived, application_archived_at, filter_reason
        ) SELECT
            id, stage, prepared_cv_set_id, application_status, applied_at,
            archived, archived_at, saved, name, type, source, link, description,
            summary, eligible, relevance, deadline, residency_start, residency_end,
            decision_date, notes, analysis_origin, created_at,
            application_archived, application_archived_at, filter_reason
        FROM opportunities;
        DROP TABLE opportunities;
        ALTER TABLE opportunities_new RENAME TO opportunities;
        CREATE INDEX opportunities_stage ON opportunities (stage, archived);
        CREATE INDEX opportunities_application ON opportunities (application_status);
        """,
    ),
    (
        7,
        "application status pending removed",
        """
        -- Removes 'pending' from the application statuses again (dsgnrfhmn, 2026-09-24:
        -- not needed). A 'pending' application becomes 'applied', which already
        -- means "awaiting a decision". Rebuilt the same way as version 6, and for
        -- the same reason it is in REBUILD_VERSIONS.
        CREATE TABLE opportunities_new (
            id                 TEXT PRIMARY KEY,
            stage              TEXT NOT NULL
                               CHECK (stage IN ('filtered', 'for_review', 'analysed')),
            prepared_cv_set_id TEXT,
            application_status TEXT
                               CHECK (application_status IN ('applied', 'approved', 'declined')),
            applied_at         TEXT,
            archived           INTEGER NOT NULL DEFAULT 0 CHECK (archived IN (0, 1)),
            archived_at        TEXT,
            saved              INTEGER NOT NULL DEFAULT 0 CHECK (saved IN (0, 1)),
            name               TEXT NOT NULL,
            type               TEXT NOT NULL CHECK (type IN ('open_call', 'grant')),
            source             TEXT NOT NULL,
            link               TEXT,
            description        TEXT NOT NULL DEFAULT '',
            summary            TEXT,
            eligible           TEXT,
            relevance          REAL,
            deadline           TEXT,
            residency_start    TEXT,
            residency_end      TEXT,
            decision_date      TEXT,
            notes              TEXT NOT NULL DEFAULT '',
            analysis_origin    TEXT CHECK (analysis_origin IN ('mock', 'live')),
            created_at         TEXT NOT NULL,
            application_archived INTEGER NOT NULL DEFAULT 0
                               CHECK (application_archived IN (0, 1)),
            application_archived_at TEXT
                               CHECK (
                                   (application_archived_at IS NOT NULL) = (application_archived = 1)
                                   AND (application_archived = 0 OR application_status IS NOT NULL)
                               ),
            filter_reason      TEXT
                               CHECK (filter_reason IS NULL OR stage = 'filtered'),
            CHECK ((archived = 1) = (archived_at IS NOT NULL)),
            CHECK ((application_status IS NULL) = (applied_at IS NULL)),
            CHECK (stage = 'analysed' OR analysis_origin IS NULL)
        );
        INSERT INTO opportunities_new (
            id, stage, prepared_cv_set_id, application_status, applied_at,
            archived, archived_at, saved, name, type, source, link, description,
            summary, eligible, relevance, deadline, residency_start, residency_end,
            decision_date, notes, analysis_origin, created_at,
            application_archived, application_archived_at, filter_reason
        ) SELECT
            id, stage, prepared_cv_set_id,
            CASE application_status WHEN 'pending' THEN 'applied' ELSE application_status END,
            applied_at,
            archived, archived_at, saved, name, type, source, link, description,
            summary, eligible, relevance, deadline, residency_start, residency_end,
            decision_date, notes, analysis_origin, created_at,
            application_archived, application_archived_at, filter_reason
        FROM opportunities;
        DROP TABLE opportunities;
        ALTER TABLE opportunities_new RENAME TO opportunities;
        CREATE INDEX opportunities_stage ON opportunities (stage, archived);
        CREATE INDEX opportunities_application ON opportunities (application_status);
        """,
    ),
    (
        8,
        "opportunity body (the intake email text, kept for analysis)",
        """
        -- The sanitized text of the email an intake record came from. Analysis
        -- reads it, so it never has to go back to the mailbox. Never part of
        -- the API's list or detail answers; `description` stays what the user typed.
        ALTER TABLE opportunities ADD COLUMN body TEXT NOT NULL DEFAULT ''
        """,
    ),
    (
        9,
        "opportunity details (the analysis blocks: overview, offer, requirements, fit, red flags, verdict)",
        """
        -- One JSON object written by analysis (pipeline/opencall_summarise.py), exposed
        -- as separate fields by the API. NULL until a real or mock analysis ran, and for
        -- rows analysed before this column existed. `summary` stays the short text.
        ALTER TABLE opportunities ADD COLUMN details TEXT
        """,
    ),
    (
        10,
        "sidebar position (the user's own order of sets)",
        """
        -- The sidebar order is the user's: drag sets where they belong, saving or
        -- archiving moves nothing. Lower comes first. Existing sets get the place
        -- they had under the earlier grouping (unsaved, saved, applied, archived,
        -- newest first in each), so the sidebar looks the same until its first drag.
        ALTER TABLE sets ADD COLUMN position INTEGER NOT NULL DEFAULT 0;
        UPDATE sets SET position = (
            SELECT r.rn FROM (
                SELECT s.id AS id,
                       ROW_NUMBER() OVER (
                           PARTITION BY s.kind
                           ORDER BY CASE WHEN s.archived = 1 THEN 3
                                         WHEN o.application_status IS NOT NULL THEN 2
                                         WHEN s.saved = 1 THEN 1 ELSE 0 END,
                                    COALESCE(s.archived_at, s.saved_at, s.created_at) DESC,
                                    s.id
                       ) - 1 AS rn
                FROM sets s LEFT JOIN opportunities o ON o.id = s.opportunity_id
            ) r WHERE r.id = sets.id
        );
        """,
    ),
    (
        11,
        "redo steps for sets",
        """
        -- One row per redo step: the content an Undo took back. Any new change
        -- or a Reset clears the set's redo steps (sets/undo.py).
        CREATE TABLE set_redo (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            set_id   TEXT NOT NULL REFERENCES sets (id) ON DELETE CASCADE,
            snapshot TEXT NOT NULL
        );
        CREATE INDEX set_redo_set ON set_redo (set_id, id);
        """,
    ),
    (
        12,
        "reply contact on applications",
        """
        -- Address or domain replies to this application come from
        -- (sources/gmail_mail_updates.py). Optional: without it a reply is
        -- matched by the opportunity's name.
        ALTER TABLE opportunities ADD COLUMN application_contact TEXT
        """,
    ),
    (
        13,
        "drop the reply contact",
        """
        -- Replies are matched by the opportunity's name in the message, sender,
        -- subject and body (dsgnrfhmn, 2026-10-23); the contact was never given a UI.
        ALTER TABLE opportunities DROP COLUMN application_contact
        """,
    ),
]

LATEST_VERSION = MIGRATIONS[-1][0]

# Migrations that rebuild a table other tables point at. They run with foreign
# keys off (the pragma is a no-op inside a transaction, so it is set on the
# connection first) and must leave foreign_key_check clean.
REBUILD_VERSIONS = {6, 7}


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)

    def _prepare_file(self) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Create with 0600 up front; sqlite then gives -wal/-shm the same mode.
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        os.close(fd)
        if (self.path.stat().st_mode & 0o077) != 0:
            raise DbError(f"{self.path} is readable by group/others; expected mode 0600.")

    def _connect(self, foreign_keys: bool = True) -> sqlite3.Connection:
        # isolation_level=None: we issue BEGIN/COMMIT ourselves.
        conn = sqlite3.connect(self.path, isolation_level=None, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA foreign_keys = {'ON' if foreign_keys else 'OFF'}")
        return conn

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def tx(self, foreign_keys: bool = True) -> Iterator[sqlite3.Connection]:
        conn = self._connect(foreign_keys)
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                if not foreign_keys:
                    # Turned off only for a table rebuild: it must leave no orphan.
                    broken = conn.execute("PRAGMA foreign_key_check").fetchall()
                    if broken:
                        raise DbError(f"foreign_key_check found {len(broken)} broken reference(s) after the rebuild.")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")
        finally:
            conn.close()

    def version(self) -> int:
        with self.read() as conn:
            return conn.execute("PRAGMA user_version").fetchone()[0]

    def migrate(self) -> int:
        """Create the file if needed and apply pending migrations. Returns the version."""
        self._prepare_file()
        with self.read() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
        current = self.version()
        if current > LATEST_VERSION:
            raise DbError(
                f"Database is at schema version {current}, newer than this code "
                f"({LATEST_VERSION}). Refusing to run against it."
            )
        for version, name, sql in MIGRATIONS:
            if version <= current:
                continue
            with self.tx(foreign_keys=version not in REBUILD_VERSIONS) as conn:
                # executescript() would COMMIT first; run statements one by one
                # so the migration and its version bump are a single transaction.
                for statement in _split(sql):
                    conn.execute(statement)
                conn.execute(f"PRAGMA user_version = {version}")
        return self.version()


def _split(script: str) -> list[str]:
    """Split a migration script into statements (no ';' inside literals allowed)."""
    lines = [ln for ln in script.splitlines() if not ln.strip().startswith("--")]
    return [s.strip() for s in "\n".join(lines).split(";") if s.strip()]
