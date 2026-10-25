"""Fake pager with the real interface.

Phase one only: two projects to a page. The real engine (layout/paginate.py,
phase two) drops in behind the same function. Every "N pages" caption is this
module's output and is wrong on purpose; nothing may depend on it being right.
"""

from __future__ import annotations

ORIGIN = "mock"
PER_PAGE = 2          # projects to a page
CV_PER_PAGE = 6       # CV blocks to a page


def paginate(entry_ids: list[str], per_page: int = PER_PAGE) -> list[list[str]]:
    """Entry ids grouped into pages, in order."""
    return [entry_ids[i:i + per_page] for i in range(0, len(entry_ids), per_page)]
