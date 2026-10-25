"""Deterministic cleanup for AI-sounding punctuation the model did not catch.

Ported from job-agent (modules/text_style.py, job-agent commit 8792296), unchanged
in behaviour. The system prompts already ask for no dashes; this is the backstop,
not a substitute for the prompt rule.
"""

from __future__ import annotations

import re

_DASH_CHARS = "–—"  # en dash, em dash
_DATE_RANGE_DASH = re.compile(rf"(?<=\d)\s*[{_DASH_CHARS}]\s*(?=\d)")
_OTHER_DASH = re.compile(rf"\s*[{_DASH_CHARS}]\s*")


def strip_ai_dashes(text: str) -> str:
    """Replace em/en dashes with plain hyphens, independent of model compliance."""
    text = _DATE_RANGE_DASH.sub("-", text)
    return _OTHER_DASH.sub(" - ", text)
