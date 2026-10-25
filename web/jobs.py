"""Heavy requests run in a worker process, so they never slow the rest of the API.

Load opportunities, Updates, Analyse, Prepare and both Edit with AI (server.HEAVY) read
mailboxes, parse pages and call Claude, for seconds or minutes. In the API process they
compete for the one interpreter lock with everything else, and the same process serves
every page and asset, so a busy job made every page load and every
click slow (dsgnrfhmn, 2026-10-24: "everything hangs until the function completes; run it in
a separate process"). Here they run in one worker process, a spawned interpreter with
its own Context built from the same Config by the same builders; the request thread only
waits for the answer, which costs the API nothing.

One worker, not more: Claude's spend guard (core/claude_client.py) counts per process,
so every Claude call has to go through the same one. Heavy requests therefore run one
after another; the rest of the API keeps answering meanwhile. The worker shares the
database (WAL) and the Drive caches on disk with the API process; DriveProjects reads
its state file again when the API process has refreshed it.

PDF exports (web/pdf_export.py, through web/exports.py) run in a second Jobs of their
own, so a long export never holds up Load opportunities; they make no Claude call, so the
spend guard stays single. They go through submit(), which answers a Future at once.

A worker that dies (killed, out of memory) fails the request it was running with a 500
and is replaced for the next one. Tests pass no Jobs to make_server: handlers then run
inline with the fakes the tests inject, as before.
"""

from __future__ import annotations

import logging
import multiprocessing
from concurrent.futures import Future, ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from threading import Lock
from typing import Any, Callable

from core.config import Config

log = logging.getLogger("arta.jobs")

_ctx = None   # the worker's own Context, built once when the worker starts


def _start(cfg: Config) -> None:
    """Worker initializer: logging like the API's, and a Context like make_server's."""
    global _ctx
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    from core.db import Database
    from web import server
    _ctx = server.build_context(cfg, Database(cfg.db_path), **server.load_runtime(cfg))
    log.info("worker ready")


def _run(name: str, method: str, path: str, params: dict, query: dict, body: Any) -> "tuple[int, Any]":
    """In the worker: the same handler the API would have run, answered as (status, payload).
    The handler goes by name and is looked up in web.server here: run as `python -m
    web.server`, the API's own functions live in __main__, a second copy of the module."""
    from web import server
    handler = getattr(server, name)
    if handler not in server.HEAVY:
        raise ValueError(f"{name} is not a heavy handler")
    req = server.Request(_ctx, method, path, params, query, body, {})
    try:
        return handler(req)
    except server.ApiError as e:
        return e.status, {"error": {"code": e.code, "message": e.message}}
    except Exception:
        log.exception("unhandled error in worker: %s %s", method, path)
        return 500, {"error": {"code": "internal", "message": "Internal error."}}


def _render_pdf(kind: str, set_id: str, out: str) -> int:
    from pathlib import Path
    from web.exports import stop_path, write_progress
    # Started: said before loading the renderer (Playwright takes a moment), so only an
    # export still waiting behind another one has no progress yet.
    write_progress(Path(out), {"phase": "drawing"})
    from web import pdf_export
    return pdf_export.render(_ctx, kind, set_id, Path(out), progress=lambda p: write_progress(Path(out), p),
                             stopped=lambda: stop_path(Path(out)).exists())


TASKS = {"render_pdf": _render_pdf}   # what submit() may run in a worker, by name


def _task(name: str, args: tuple) -> Any:
    return TASKS[name](*args)


class Jobs:
    def __init__(self, cfg: Config):
        self._cfg = cfg
        self._lock = Lock()
        self._pool = self._new_pool()

    def _new_pool(self) -> ProcessPoolExecutor:
        # spawn: a fresh interpreter, not a fork of a process that has threads running.
        pool = ProcessPoolExecutor(max_workers=1, mp_context=multiprocessing.get_context("spawn"),
                                   initializer=_start, initargs=(self._cfg,))
        pool.submit(int)    # start the worker now, so the first heavy request does not wait for it
        return pool

    def run(self, handler: Callable, method: str, path: str, params: dict, query: dict, body: Any) -> "tuple[int, Any]":
        with self._lock:
            pool = self._pool
        try:
            return pool.submit(_run, handler.__name__, method, path, params, query, body).result()
        except BrokenProcessPool:
            log.error("the worker process stopped during %s %s; starting a new one", method, path)
            with self._lock:
                if self._pool is pool:
                    self._pool = self._new_pool()
            return 500, {"error": {"code": "internal", "message": "The background worker stopped. Try again."}}

    def submit(self, name: str, *args: Any) -> Future:
        """Run TASKS[name](*args) in the worker; the Future fails with BrokenProcessPool if
        the worker dies, and the next submit gets a new one."""
        if name not in TASKS:
            raise ValueError(f"No task {name!r}.")
        with self._lock:
            pool = self._pool
        try:
            return pool.submit(_task, name, args)
        except BrokenProcessPool:
            log.error("the worker process had stopped; starting a new one for %s", name)
            with self._lock:
                if self._pool is pool:
                    self._pool = self._new_pool()
                pool = self._pool
            return pool.submit(_task, name, args)

    def close(self) -> None:
        with self._lock:
            self._pool.shutdown(wait=False, cancel_futures=True)
