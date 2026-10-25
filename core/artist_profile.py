"""The artist profile: trusted context for analysis (CLAUDE.md, Credentials).

One plain-text file, written by hand from what the artist said about herself:
eligibility facts, practice, what she seeks, red flags. It is personal data, so it
lives outside the repository tree, mode 0600, like the other secrets, and its
content is never logged or echoed. The model reads it as the one source for
"eligible" and "relevance" (pipeline/opencall_summarise.py).

It is trusted, unlike an email body. It still goes into the prompt as text only:
nothing in it is executed or selects an action.
"""

from __future__ import annotations

from pathlib import Path

from core.config import REPO_ROOT

PROFILE_FILE_VAR = "llm.artist_profile_file"
MAX_CHARS = 8_000   # about 2k tokens; a longer file is a mistake, not a profile


class ProfileError(Exception):
    """The profile file is missing or unusable. The message is user-facing and names
    the path and the permissions, never the content."""


def load(path: Path | None, repo_root: Path = REPO_ROOT) -> str:
    """The profile text. Fail loud: unset, missing, inside the repository tree,
    readable by group/others, empty or too long."""
    if path is None:
        raise ProfileError(f"{PROFILE_FILE_VAR} is not set. Point it at the artist profile file, mode 0600.")
    if not path.is_absolute():
        raise ProfileError(f"{PROFILE_FILE_VAR}={str(path)!r} must be an absolute path.")
    resolved, root = path.resolve(), repo_root.resolve()
    if root in resolved.parents:
        raise ProfileError(f"{PROFILE_FILE_VAR}={str(path)!r} is inside the repository tree. Choose a path outside it.")
    if not path.is_file():
        raise ProfileError(f"{path} not found. Create the artist profile there and chmod 600.")
    if path.stat().st_mode & 0o077:
        raise ProfileError(f"{path} is accessible to other users. Run: chmod 600 {path}")
    try:
        text = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        raise ProfileError(f"{path} could not be read as UTF-8 text.") from None
    if not text:
        raise ProfileError(f"{path} is empty.")
    if len(text) > MAX_CHARS:
        raise ProfileError(f"{path} is {len(text)} characters; the limit is {MAX_CHARS}.")
    return text
