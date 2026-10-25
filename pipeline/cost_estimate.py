"""The figure in the API cost confirmation popup (spec 3.7).

Computed per invocation from the items and the call each one makes: the size of
the real prompt, and an assumed size of the answer. Never a hardcoded string.

It is an ESTIMATE, and says so. The rates below are placeholders: nobody has
checked them against a real invoice line, and that check takes a live call that
needs dsgnrfhmn's go. An estimator that is consistently wrong is worse than no
number, so the basis travels with the figure and the popup does not present it as fact.
"""

from __future__ import annotations

import math
from typing import Any

from core.claude_client import RATE_INPUT_PER_MTOK, RATE_OUTPUT_PER_MTOK, LLMRequest, estimate_tokens

# The rates are PLACEHOLDERS, unverified against an invoice; they live in core.claude_client so
# the estimate and the live client's spend guard cannot drift apart.
# What an answer is assumed to take, per action.
ASSUMED_OUTPUT_TOKENS = {"analyse": 300, "prepare": 400, "edit_portfolio": 1500, "edit_cv": 1500}
BASIS = "Estimate from prompt size and placeholder rates, not yet checked against a real invoice."


def estimate(action: str, requests: list[LLMRequest], mocked: bool) -> dict[str, Any]:
    """One model call per request. `usd` is rounded up to the cent."""
    if action not in ASSUMED_OUTPUT_TOKENS:
        raise ValueError(f"action must be one of {', '.join(ASSUMED_OUTPUT_TOKENS)}.")
    tokens_in = sum(estimate_tokens(r.system) + estimate_tokens(r.user) for r in requests)
    tokens_out = ASSUMED_OUTPUT_TOKENS[action] * len(requests)
    usd = (tokens_in * RATE_INPUT_PER_MTOK + tokens_out * RATE_OUTPUT_PER_MTOK) / 1_000_000
    return {
        "action": action,
        "items": len(requests),
        "calls": len(requests),
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "usd": math.ceil(usd * 100) / 100 if requests else 0.0,
        "mocked": mocked,
        "estimate": True,
        "basis": BASIS,
    }
