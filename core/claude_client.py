"""The Claude client: a mock that costs nothing, and a live one that spends money.

The default is the mock (llm.mode = mock). The live client exists only for
llm.mode = live with a key and a model name; it is never reached otherwise, and
it never falls back to the mock. The mock never falls forward to the network:
it imports no HTTP code and opens no socket.

Both answer a request with the model's raw text. What that text means (a JSON
object with the analysis fields, a list of block ids) is decided by the caller,
which validates it. Output only fills fields; nothing here selects an action.

Mock output is labelled: `origin` is "mock" and the pipeline stores it, so a mock
result can never be reported as a real analysis.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import sqlite3
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from core.config import Config, ConfigError, read_secret

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
MAX_OUTPUT_TOKENS = 1024

# USD per million tokens. PLACEHOLDERS, unverified until checked against a real
# invoice. Used by the cost estimate and by the live client's spend guard.
RATE_INPUT_PER_MTOK = 3.0
RATE_OUTPUT_PER_MTOK = 15.0

log = logging.getLogger("arta.llm")


class ClientError(Exception):
    """The call failed. The message is user-facing: it names what failed, never a
    key, a header or a raw response."""


@dataclass(frozen=True)
class LLMRequest:
    task: str                      # "analysis" | "adapt_cv"
    system: str
    user: str                      # untrusted content already delimited by prompt_guard
    # Which record this is about. The live client ignores it; the mock uses it to find
    # canned output and to derive a deterministic answer.
    meta: Mapping[str, Any] = field(default_factory=dict)
    max_tokens: int = MAX_OUTPUT_TOKENS


class Client(Protocol):
    origin: str                    # "mock" | "live", stored with what it produced

    def complete(self, request: LLMRequest) -> str:
        """The model's raw text. Raises ClientError."""


# --- mock ---------------------------------------------------------------------

def _iso(d: date) -> str:
    return d.isoformat()


class MockClient:
    """Canned, schema-valid output from tests/fixtures/llm/cases.json, and a
    deterministic answer derived from the request's meta for everything else.

    `calls` records every request, so a test can prove how many calls a flow made
    (none that nothing asked for)."""

    origin = "mock"

    def __init__(self, cases_path: Path | None = None):
        self.cases: dict[str, dict[str, Any]] = {}
        if cases_path is not None:
            self.cases = json.loads(Path(cases_path).read_text("utf-8"))
        self.calls: list[LLMRequest] = []

    def complete(self, request: LLMRequest) -> str:
        self.calls.append(request)
        key = str(request.meta.get("id", ""))
        case = self.cases.get(request.task, {}).get(key)
        if case is not None:
            if "error" in case:
                raise ClientError(case["error"])
            return json.dumps(case["output"])
        make = {"analysis": self._analysis, "adapt_cv": self._adapt_cv,
                "edit_portfolio": self._edit_portfolio, "edit_cv": self._edit_cv}.get(request.task)
        if make is None:
            raise ClientError(f"The mock has no answer for the task {request.task!r}.")
        return json.dumps(make(request.meta))

    @staticmethod
    def _analysis(meta: Mapping[str, Any]) -> dict[str, Any]:
        """Plausible fields for any record, from its id and creation date. Made up on
        purpose and labelled mock wherever it is shown."""
        h = int(hashlib.sha256(str(meta.get("id", "")).encode()).hexdigest(), 16)
        try:
            base = date.fromisoformat(str(meta.get("created_at", ""))[:10])
        except ValueError:
            base = date(2026, 9, 1)
        deadline = base + timedelta(days=45 + h % 40)
        out: dict[str, Any] = {
            "type": meta.get("type", "open_call"),
            "summary": "Mock analysis: a plausible fit, worth a closer look.",
            "eligible": ("yes", "yes", "unclear")[h % 3],
            "relevance": round(0.5 + (h % 50) / 100, 2),
            "deadline": _iso(deadline),
            "residency_start": None,
            "residency_end": None,
            "decision_date": None,
            "details": {
                "overview": "Mock analysis: an opportunity for artists.",
                "offer": "Mock: terms not read.",
                "requirements": "Mock: conditions not read.",
                "focus": "Mock: no focus extracted.",
                "fit": "Mock: not compared with the profile.",
                "red_flags": "None.",
                "verdict": "maybe",
                "verdict_reason": "Mock output, check the real text.",
            },
        }
        if out["type"] == "grant":
            out["decision_date"] = _iso(deadline + timedelta(days=60 + h % 30))
        else:
            start = deadline + timedelta(days=90)
            out["residency_start"], out["residency_end"] = _iso(start), _iso(start + timedelta(days=60))
        return out

    @staticmethod
    def _adapt_cv(meta: Mapping[str, Any]) -> dict[str, Any]:
        """A fixed pick from the blocks offered: a grant takes nearly all of them, an
        open call the first part. Ids only, never text."""
        ids: list[str] = list(meta.get("available_ids", []))
        keep = max(1, len(ids) - 3) if meta.get("type") == "grant" else max(1, len(ids) // 2)
        return {"block_ids": ids[:keep]}

    @staticmethod
    def _edit_portfolio(meta: Mapping[str, Any]) -> dict[str, Any]:
        """Stands in for a real edit: the same entries in reverse order, nothing else
        touched, so the preview visibly changes and one undo restores it. The request's
        words are not read. A set of one or none comes back unchanged."""
        projects = list(meta.get("entry_projects", []))
        return {"entries": [{"entry": str(n), "project": str(projects[n - 1])} for n in range(len(projects), 0, -1)]}

    @staticmethod
    def _edit_cv(meta: Mapping[str, Any]) -> dict[str, Any]:
        """Stands in for a real edit: the set's entries in reverse order (headings follow
        their entries), no text touched, so the preview visibly changes and one undo restores
        it. The request's words are not read. A set of one entry comes back unchanged."""
        return {"blocks": [{"block": n} for n in reversed(list(meta.get("set_entries", [])))]}


# --- live -----------------------------------------------------------------------

class LiveClient:
    """The real API, over stdlib HTTP. Reached only with llm.mode = live. Not used
    by any test against the network; the tests check the request it would send."""

    origin = "live"

    def __init__(
        self,
        api_key: str,
        model: str,
        opener: Callable[..., Any] = urllib.request.urlopen,
        timeout: float = 60.0,
        max_usd: float | None = None,
        max_calls: int | None = None,
    ):
        if not api_key or not model:
            raise ClientError("The live client needs an API key and a model name.")
        self._key, self._model, self._open, self._timeout = api_key, model, opener, timeout
        self.model = model
        # Spend guard, per process. A call that would pass either limit is refused
        # before it is sent; there is no way to turn it into a warning.
        self.max_usd, self.max_calls = max_usd, max_calls
        self.calls, self.spent_usd = 0, 0.0
        self._lock = threading.Lock()

    def request_body(self, request: LLMRequest) -> dict[str, Any]:
        return {
            "model": self._model,
            "max_tokens": request.max_tokens,
            "system": request.system,
            "messages": [{"role": "user", "content": request.user}],
        }

    def _guard(self, request: LLMRequest) -> None:
        """Refuse the call that would pass a limit, using the worst case for its output."""
        if self.max_calls is not None and self.calls >= self.max_calls:
            raise ClientError(f"Call limit reached ({self.max_calls}); raise llm.max_calls to go on.")
        if self.max_usd is not None:
            worst = (estimate_tokens(request.system) + estimate_tokens(request.user)) * RATE_INPUT_PER_MTOK
            worst += request.max_tokens * RATE_OUTPUT_PER_MTOK
            if self.spent_usd + worst / 1_000_000 > self.max_usd:
                raise ClientError(f"Spend limit ${self.max_usd:.2f} would be passed; raise llm.max_usd to go on.")

    def _account(self, payload: Mapping[str, Any], request: LLMRequest) -> None:
        """Count the call and log its token usage: ids and numbers, never text."""
        usage = payload.get("usage") if isinstance(payload, Mapping) else None
        usage = usage if isinstance(usage, Mapping) else {}
        tin, tout = int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0)
        cw = int(usage.get("cache_creation_input_tokens") or 0)
        cr = int(usage.get("cache_read_input_tokens") or 0)
        usd = ((tin + cw) * RATE_INPUT_PER_MTOK + tout * RATE_OUTPUT_PER_MTOK) / 1_000_000
        self.calls += 1
        self.spent_usd += usd
        stop = payload.get("stop_reason") if isinstance(payload, Mapping) else None
        log.info(
            "llm call task=%s id=%s model=%s in=%d out=%d cache_write=%d cache_read=%d stop=%s "
            "usd~%.4f total_usd~%.4f calls=%d",
            request.task, request.meta.get("id", "-"), self.model, tin, tout, cw, cr, stop, usd, self.spent_usd,
            self.calls,
        )
        if stop == "max_tokens":
            # Counted and logged above (it was paid for), then refused: a cut-off answer
            # is never parsed, and the caching wrapper never stores it.
            raise ClientError(
                f"The answer was cut off at the {request.max_tokens}-token limit. Nothing was stored."
            )

    def complete(self, request: LLMRequest) -> str:
        with self._lock:
            return self._complete(request)

    def _complete(self, request: LLMRequest) -> str:
        self._guard(request)
        http = urllib.request.Request(
            API_URL,
            data=json.dumps(self.request_body(request)).encode("utf-8"),
            method="POST",
            headers={"content-type": "application/json", "x-api-key": self._key, "anthropic-version": API_VERSION},
        )
        try:
            with self._open(http, timeout=self._timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            self._account(payload, request)
            return "".join(b["text"] for b in payload["content"] if b.get("type") == "text")
        except urllib.error.HTTPError as e:
            raise ClientError(f"The Claude API answered {e.code}.") from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise ClientError("The Claude API could not be reached.") from None
        except (KeyError, TypeError, ValueError):
            raise ClientError("The Claude API sent an unreadable reply.") from None


def make_client(cfg: Config) -> Client:
    """The client the configuration asks for. Live needs the key file and the model, and
    says so loudly when either is missing; it does not become the mock."""
    if cfg.llm == "mock":
        return MockClient(cfg.fixtures_dir / "llm" / "cases.json")
    if cfg.api_key_file is None:
        raise ClientError("llm.mode = \"live\" needs llm.api_key_file.")
    if not cfg.model:
        raise ClientError("llm.mode = \"live\" needs a model name.")
    try:
        key = read_secret("llm.api_key_file", cfg.api_key_file)
    except ConfigError as e:
        raise ClientError(str(e)) from None
    live = LiveClient(key, cfg.model, max_usd=cfg.llm_max_usd, max_calls=cfg.llm_max_calls)
    if not cfg.llm_cache:
        return live
    return CachingClient(live, cfg.llm_cache_path)


# --- cache ----------------------------------------------------------------------

# prompt_guard puts a random 16-hex nonce in every marker. It is not content, and
# left in it would make every key new.
_NONCE = re.compile(r"(<<<(?:UNTRUSTED|END) [a-z_]+ )[0-9a-f]{16}(>>>)")


class CachingClient:
    """Answers of a live client, kept by content so the same text is paid for once.

    Only for tasks listed in CACHED_TASKS (analysis; adapt_cv stays uncached). The key
    is the model, the task, the system prompt, the request text with marker nonces
    normalized, and max_tokens: a changed email, a changed prompt or a changed model is
    a miss. Only the model's answer is stored, never the request. Errors are never
    stored, and `discard` drops an answer the caller found invalid, so a bad answer is
    not served again.

    The file lives outside the repo tree, mode 0600. It is a performance layer only:
    deleting it costs money, never correctness.
    """

    CACHED_TASKS = ("analysis",)
    origin = "live"

    def __init__(self, inner: LiveClient, path: Path):
        self.inner = inner
        self.model = inner.model
        self.hits = 0
        self._lock = threading.Lock()
        path = Path(path)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS llm_cache (key TEXT PRIMARY KEY, task TEXT NOT NULL,"
            " model TEXT NOT NULL, response TEXT NOT NULL, created_at TEXT NOT NULL)"
        )
        self._db.commit()
        os.chmod(path, 0o600)

    # delegated so callers can read the totals
    @property
    def calls(self) -> int:
        return self.inner.calls

    @property
    def spent_usd(self) -> float:
        return self.inner.spent_usd

    def key(self, request: LLMRequest) -> str:
        user = _NONCE.sub(r"\1N\2", request.user)
        blob = json.dumps([self.model, request.task, request.system, user, request.max_tokens], ensure_ascii=False)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def complete(self, request: LLMRequest) -> str:
        if request.task not in self.CACHED_TASKS:
            return self.inner.complete(request)
        key = self.key(request)
        with self._lock:
            row = self._db.execute("SELECT response FROM llm_cache WHERE key = ?", (key,)).fetchone()
            if row is not None:
                self.hits += 1
                log.info("llm cache hit task=%s id=%s hits=%d", request.task, request.meta.get("id", "-"), self.hits)
                return row[0]
        text = self.inner.complete(request)
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO llm_cache (key, task, model, response, created_at) VALUES (?, ?, ?, ?, ?)",
                (key, request.task, self.model, text, datetime.now(timezone.utc).isoformat(timespec="seconds")),
            )
            self._db.commit()
        return text

    def discard(self, request: LLMRequest) -> None:
        with self._lock:
            self._db.execute("DELETE FROM llm_cache WHERE key = ?", (self.key(request),))
            self._db.commit()


def estimate_tokens(text: str) -> int:
    """About four characters to a token. A rough figure, used only for the cost estimate."""
    return math.ceil(len(text) / 4)
