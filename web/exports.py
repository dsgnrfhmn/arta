"""PDF exports in flight and done, in the API process (dsgnrfhmn, 2026-10-24).

Download PDF starts an export and answers at once with its id (POST
/api/sets/<kind>/<id>/pdf); the page asks for its state (GET /api/exports/<job>) until it
is done or failed, then fetches the file (GET /api/exports/<job>/file). An export reads
every original from Drive, which takes longer than a request should stay open
(minutes), so it cannot be one request. While it runs, its state carries the progress the
renderer reports (write_progress: {phase, done?, total?}), so the page can show it.

Exports belong to the dashboard, not to a page (dsgnrfhmn, 2026-10-25): a page that is left
does not stop one, and every page asks GET /api/exports on load to follow what is still
running and to offer what finished meanwhile (a toast that stays across pages). So:
starting a set that is already being exported answers that export instead of queueing a
second; list() holds the running ones, the finished ones not downloaded yet, and the
failed ones until a page has been told (state() marks them).

stop() (DELETE /api/exports/<job>, the toast's close button after a confirmation) ends
one: an export still waiting for the worker is taken off the queue; a running one gets a
stop file next to its PDF, which the renderer checks while the page draws and before it
prints (web/pdf_export.py), so it ends within a moment. A stopped export is not listed.

The rendering runs elsewhere, through `submit(kind, set_id, path) -> Future` (the export
worker in production, see web/jobs.py; a thread in tests). The finished file is written
to `directory` (data_dir/exports, 0700, files 0600), outside the repository, and is
deleted TTL seconds after it was started, downloaded or not. The ids are random and only
answered behind the dashboard's sign-in, like every API route.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

log = logging.getLogger("arta.exports")

TTL = 30 * 60


@dataclass
class Export:
    id: str
    kind: str
    set_id: str
    name: str
    path: Path
    started: float
    future: Future
    downloaded: bool = False     # the file was fetched once
    told: bool = False           # a failure was reported to a page

    def state(self) -> dict:
        """{status: running, phase?, done?, total?} (no phase yet: waiting for the worker),
        {status: done, pages}, {status: stopped} or {status: error, message}."""
        if not self.future.done():
            if stop_path(self.path).exists():
                return {"status": "running", "phase": "stopping"}
            return {"status": "running", **read_progress(self.path)}
        progress_path(self.path).unlink(missing_ok=True)
        stop_path(self.path).unlink(missing_ok=True)
        if self.stopped:
            self.path.unlink(missing_ok=True)
            return {"status": "stopped"}
        error = self.future.exception()
        if error is None:
            return {"status": "done", "pages": self.future.result()}
        self.told = True
        return {"status": "error", "message": _message(error)}

    def summary(self) -> dict:
        return {"job": self.id, "kind": self.kind, "set_id": self.set_id, "name": self.name, **self.state()}

    @property
    def stopped(self) -> bool:
        from web.pdf_export import ExportStopped
        return self.future.cancelled() or (self.future.done() and isinstance(self.future.exception(), ExportStopped))

    @property
    def open(self) -> bool:
        """Still worth showing: running, done and not downloaded, failed and not told."""
        if not self.future.done():
            return True
        if self.stopped:
            return False
        if self.future.exception() is None:
            return not self.downloaded
        return not self.told


def progress_path(pdf: Path) -> Path:
    return Path(pdf).with_suffix(".progress.json")


def stop_path(pdf: Path) -> Path:
    return Path(pdf).with_suffix(".stop")


def write_progress(pdf: Path, progress: dict) -> None:
    """Called by the renderer, in whatever process it runs: the progress next to the PDF
    it is writing, replaced whole so a reader never sees half of it."""
    path = progress_path(pdf)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(progress), "utf-8")
    os.replace(tmp, path)


def read_progress(pdf: Path) -> dict:
    """{phase, done, total} as last written; {} before the first report."""
    try:
        data = json.loads(progress_path(pdf).read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: data[k] for k in ("phase", "done", "total") if k in data}


def _message(error: BaseException) -> str:
    from web.pdf_export import ExportError
    if isinstance(error, ExportError):
        return str(error)
    log.error("pdf export failed: %s: %s", type(error).__name__, error)
    return "The PDF could not be made."


def filename(name: str) -> str:
    """The set's name as a file name: no path or control characters, .pdf at the end."""
    clean = re.sub(r'[\x00-\x1f\x7f/\\:*?"<>|]+', " ", name).strip(" .") or "set"
    return clean[:120] + ".pdf"


class Exports:
    def __init__(self, directory: Path, submit: Callable[[str, str, str], Future],
                 ttl: float = TTL, clock: Callable[[], float] = time.time):
        self.directory = Path(directory)
        self._submit, self._ttl, self._clock = submit, ttl, clock
        self._jobs: dict[str, Export] = {}
        self._lock = threading.Lock()

    def start(self, kind: str, set_id: str, name: str) -> Export:
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._expire()
        with self._lock:
            running = next((j for j in self._jobs.values()
                            if j.kind == kind and j.set_id == set_id and not j.future.done()), None)
        if running is not None:
            return running
        job_id = secrets.token_hex(12)
        path = self.directory / f"{job_id}.pdf"
        export = Export(job_id, kind, set_id, name, path, self._clock(), self._submit(kind, set_id, str(path)))
        with self._lock:
            self._jobs[job_id] = export
        return export

    def stop(self, job_id: str) -> Export | None:
        """End an export: off the queue if it waits, else ask the renderer to stop."""
        job = self.get(job_id)
        if job is None or job.future.done():
            return job
        if not job.future.cancel():
            stop_path(job.path).touch()
        return job

    def list(self) -> list[Export]:
        """The exports a page should show, oldest first."""
        self._expire()
        with self._lock:
            jobs = sorted(self._jobs.values(), key=lambda j: j.started)
        return [j for j in jobs if j.open]

    def get(self, job_id: str) -> Export | None:
        self._expire()
        with self._lock:
            return self._jobs.get(job_id)

    def _expire(self) -> None:
        now = self._clock()
        with self._lock:
            old = [j for j in self._jobs.values() if now - j.started > self._ttl and j.future.done()]
            for job in old:
                del self._jobs[job.id]
        for job in old:
            job.path.unlink(missing_ok=True)
            progress_path(job.path).unlink(missing_ok=True)
            stop_path(job.path).unlink(missing_ok=True)
        # Files of exports this process no longer knows (a restart) go the same way.
        if self.directory.is_dir():
            for f in [*self.directory.glob("*.pdf"), *self.directory.glob("*.progress.json"), *self.directory.glob("*.stop")]:
                if now - f.stat().st_mtime > self._ttl and f.name.split(".")[0] not in self._jobs:
                    f.unlink(missing_ok=True)
