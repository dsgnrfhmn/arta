"""CvSource over the master CV on Drive (cv.drive_file), with a local cache.

The master is a Google Doc, exported to OpenDocument text on every import and parsed by
pipeline/odt_cv. Drive is the source of truth for the text. The cache holds the last
import (blocks with their ids and sections) and exists for two things: block ids must
survive a re-import (pipeline/cv_blocks merges by text, so same text keeps its id), and
a request must not export the file every time.

Freshness: at most every `ttl` seconds the file's metadata (version) is asked of Drive.
A changed version means export, parse, merge, rewrite the cache. An unchanged version
means the cache is current. If Drive cannot be asked (revoked token, no network) the call
fails loudly with SourceError. A stale cache is never served as if it were live.

Short, Middle and Full are the same list for now: the whole master. They stay three
referents so the API and the sets keep their shape; a split into sections comes back as
a decision, not as a default.

Read-only: Drive read scope, no write, no mail.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pipeline.cv_blocks import CvImportError, import_blocks
from pipeline.odt_cv import parse
from sources.cv_source import Block, Referent
from sources.project_source import NotFound, SourceError

REFERENT_NAMES = (("short", "Short CV"), ("middle", "Middle CV"), ("full", "Full CV"))
GDOC_MIME = "application/vnd.google-apps.document"
ODT_MIME = "application/vnd.oasis.opendocument.text"
DOWNLOAD_MAX = 10_000_000


class DriveCv:
    def __init__(
        self,
        file_id: str,
        cache_path: Path,
        service: Callable[[], Any],
        ttl: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.file_id = file_id
        self.cache_path = Path(cache_path)
        self._service = service
        self.ttl = ttl
        self._clock = clock
        self._checked_at: float | None = None
        self._state: dict | None = None
        self.last_skipped: list[str] = []

    # -- CvSource ----------------------------------------------------------

    def list_referents(self) -> list[Referent]:
        return [Referent(rid, name) for rid, name in REFERENT_NAMES]

    def get_blocks(self, referent_id: str) -> list[Block]:
        if referent_id not in dict(REFERENT_NAMES):
            raise NotFound(f"No such referent: {referent_id}")
        return [Block(e["id"], e["kind"], e["text"]) for e in self._current()["blocks"]]

    # -- Drive and cache -----------------------------------------------------

    def _drive(self):
        return self._service()

    def _current(self) -> dict:
        now = self._clock()
        if self._state is not None and self._checked_at is not None and now - self._checked_at < self.ttl:
            return self._state
        cached = self._read_cache()
        try:
            svc = self._drive()
            meta = svc.files().get(
                fileId=self.file_id, fields="id,mimeType,version", supportsAllDrives=True
            ).execute()
            if meta.get("mimeType") != GDOC_MIME or not meta.get("version"):
                raise SourceError("The master CV on Drive is not a Google Doc.")
            if cached is None or cached.get("version") != meta["version"]:
                cached = self._import(svc, meta["version"], cached)
        except SourceError:
            raise
        except CvImportError as exc:
            raise SourceError(f"The master CV on Drive cannot be imported: {exc}") from None
        except Exception as exc:  # google client, auth and network errors: no detail that could hold a token
            raise SourceError(f"Drive could not be read ({type(exc).__name__}). Nothing stale is served.") from None
        self._state, self._checked_at = cached, now
        return cached

    def _import(self, svc, version: str, cached: dict | None) -> dict:
        data = svc.files().export(fileId=self.file_id, mimeType=ODT_MIME).execute()
        if len(data) > DOWNLOAD_MAX:
            raise SourceError("The master CV on Drive is larger than expected.")
        parsed = parse(data)
        existing = [Block(e["id"], e["kind"], e["text"]) for e in (cached or {}).get("blocks", [])]
        result = import_blocks([e for _, e in parsed.items], existing)
        state = {
            "file_id": self.file_id,
            "version": version,
            "blocks": [
                {**b.to_api(), "section": sec} for (sec, _), b in zip(parsed.items, result.blocks)
            ],
            # Reported, not deleted: sets keep their own copy of a removed block's text.
            "removed": [b.id for b in result.removed],
        }
        self.last_skipped = parsed.skipped
        self._write_cache(state)
        return state

    def _read_cache(self) -> dict | None:
        try:
            state = json.loads(self.cache_path.read_text("utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            raise SourceError(f"The CV cache {self.cache_path} is unreadable. Delete it to re-import.") from None
        if state.get("file_id") != self.file_id:
            return None   # a different file: its ids are not ours to carry over
        return state

    def _write_cache(self, state: dict) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.cache_path.parent, prefix=".cv_master.")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.cache_path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
