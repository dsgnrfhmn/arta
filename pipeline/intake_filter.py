"""Intake pre-filter: items that are plainly not open calls land in Filtered, not For Review.

Same philosophy as job-agent's filters.yaml: a denylist, deliberately coarse, run
before any paid step. It judges only what is cheap and unambiguous, the sender
(address or domain) and the subject, and never the body. One email is one record
here and a digest holds many calls, so a body match on "music" would drop a whole
newsletter for one item in it.

The rules are data (config/intake_filters.json), not code. Each rule has a
`reason` and any of four lists; it fires when any one of them matches:

  sender_domains      the sender's domain or a subdomain of it ("google.com"
                      matches "accounts.google.com", not "notgoogle.com")
  sender_addresses    the exact sender address
  subject_words       whole words in the subject (short words that would
                      otherwise collide inside longer ones)
  subject_substrings  plain substrings in the subject (compound words with no
                      separator, other alphabets)

An art field to filter out (music, say) is just another rule. JSON, not YAML:
the server image carries no YAML library, and nothing else here needs one.

Two actions. "filter" (the default) stores the item in Filtered with the rule's
reason, so it can be promoted. "drop" stores nothing: the item is marked seen so
it is not read again, and it never reaches the dashboard in any form. Drop is for
mail that is never an opportunity (Google's own notices).

`force_accept_substrings` is a top-level list: a subject containing one (phrase
starts at a word boundary, so "fine art" matches "Fine Arts" and not "Redefine
Art") is let through every "filter" rule, music included. It does not override
"drop": a mail from a dropped sender is dropped whatever its subject says.

A broken file raises FilterConfigError at start; a filter that "loads" but does
nothing is the failure this is built to avoid.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

LISTS = ("sender_domains", "sender_addresses", "subject_words", "subject_substrings")
ACTIONS = ("filter", "drop")


class FilterConfigError(ValueError):
    """The filter file is missing or invalid. The message is user-facing."""


@dataclass(frozen=True)
class Verdict:
    action: str      # "filter" or "drop"
    reason: str


@dataclass(frozen=True)
class Rule:
    reason: str
    action: str = "filter"
    sender_domains: tuple[str, ...] = ()
    sender_addresses: tuple[str, ...] = ()
    subject_words: tuple[str, ...] = ()
    subject_substrings: tuple[str, ...] = ()


class IntakeFilters:
    def __init__(self, rules: list[Rule], force_accept: tuple[str, ...] = ()):
        self.rules = tuple(rules)
        self.force_accept = force_accept
        self._accept = re.compile("|".join(r"(?<!\w)" + re.escape(t) for t in force_accept), re.IGNORECASE) if force_accept else None
        self._words = [self._compile(r.subject_words, True) for r in self.rules]
        self._subs = [self._compile(r.subject_substrings, False) for r in self.rules]

    @staticmethod
    def _compile(terms: tuple[str, ...], whole_word: bool) -> "re.Pattern | None":
        if not terms:
            return None
        body = "|".join(re.escape(t) for t in terms)
        return re.compile(rf"(?<!\w)(?:{body})(?!\w)" if whole_word else body, re.IGNORECASE)

    def check(self, sender_address: str, subject: str) -> "Verdict | None":
        """What to do with an item, or None to let it through. A drop rule is judged
        first and cannot be overridden; then force-accept; then the filter rules. The
        reason is built from the rule's own text, never from the email."""
        accepted = self._accept is not None and self._accept.search(subject or "") is not None
        for action in ("drop", "filter"):
            if action == "filter" and accepted:
                return None
            for rule, words, subs in zip(self.rules, self._words, self._subs):
                if rule.action == action and (why := self._match(rule, words, subs, sender_address, subject)):
                    return Verdict(action, f"{rule.reason} ({why})")
        return None

    @staticmethod
    def _match(rule: Rule, words, subs, sender_address: str, subject: str) -> "str | None":
        address = (sender_address or "").strip().lower()
        domain = address.rpartition("@")[2] if "@" in address else ""
        if address and address in rule.sender_addresses:
            return f"sender {address}"
        for d in rule.sender_domains:
            if domain and (domain == d or domain.endswith("." + d)):
                return f"sender domain {d}"
        for pattern in (words, subs):
            if pattern is not None:
                m = pattern.search(subject or "")
                if m:
                    return f"subject: {m.group(0).lower()}"
        return None


def parse(data: object) -> IntakeFilters:
    if not isinstance(data, dict) or set(data) - {"rules", "notes", "force_accept_substrings"} or not isinstance(data.get("rules"), list):
        raise FilterConfigError('The filter file must be {"rules": [...]} (and optionally "notes").')
    rules = []
    for i, raw in enumerate(data["rules"], 1):
        where = f"rule {i}"
        if not isinstance(raw, dict):
            raise FilterConfigError(f"{where}: must be an object.")
        unknown = set(raw) - {"reason", "action", *LISTS}
        if unknown:
            raise FilterConfigError(f"{where}: unknown keys {sorted(unknown)}.")
        reason = raw.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise FilterConfigError(f"{where}: needs a non-empty reason.")
        action = raw.get("action", "filter")
        if action not in ACTIONS:
            raise FilterConfigError(f"{where}: action must be one of {', '.join(ACTIONS)}, not {action!r}.")
        lists = {}
        for name in LISTS:
            terms = raw.get(name, [])
            if not isinstance(terms, list) or not all(isinstance(t, str) and t.strip() for t in terms):
                raise FilterConfigError(f"{where}: {name} must be a list of non-empty strings.")
            lists[name] = tuple(t.strip().lower() for t in terms)
        if not any(lists.values()):
            raise FilterConfigError(f"{where} ({reason!r}): has no terms, so it would do nothing.")
        for d in lists["sender_domains"]:
            if "@" in d:
                raise FilterConfigError(f"{where}: sender_domains takes a domain, not an address: {d!r}.")
        for a in lists["sender_addresses"]:
            if "@" not in a:
                raise FilterConfigError(f"{where}: sender_addresses takes a full address: {a!r}.")
        rules.append(Rule(reason.strip(), action, **lists))
    if not rules:
        raise FilterConfigError("The filter file has no rules.")
    accept = data.get("force_accept_substrings", [])
    if not isinstance(accept, list) or not all(isinstance(t, str) and t.strip() for t in accept):
        raise FilterConfigError("force_accept_substrings must be a list of non-empty strings.")
    return IntakeFilters(rules, tuple(t.strip().lower() for t in accept))


def load(path: Path) -> IntakeFilters:
    try:
        data = json.loads(Path(path).read_text("utf-8"))
    except OSError as e:
        raise FilterConfigError(f"The intake filter file could not be read: {e.strerror or e}.") from None
    except ValueError as e:
        raise FilterConfigError(f"The intake filter file is not valid JSON: {e}.") from None
    return parse(data)
