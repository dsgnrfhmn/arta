"""Prepare Application: one CV set and one portfolio set for an opportunity (spec 4.3.1).

The CV set holds the blocks pipeline/adapt_cv chose, with the ids they have in the
master. The portfolio set is created empty: choosing projects for an opportunity is
adapt_portfolio, which is phase two. Both are linked to the opportunity, named
"<name>: CV" and "<name>: Portfolio", and the record remembers its CV set.
"""

from __future__ import annotations

import sqlite3

from models import opportunity
from models.set import NAME_MAX
from sets import store
from sources.cv_source import Block


def _name(opportunity_name: str, suffix: str) -> str:
    room = NAME_MAX - len(suffix) - 2
    return f"{opportunity_name.strip()[:room].rstrip()}: {suffix}"


def create_sets(conn: sqlite3.Connection, rec: opportunity.Opportunity, blocks: list[Block], now: str) -> tuple[str, str]:
    """Returns (cv_set_id, portfolio_set_id). Refuses a record that cannot be prepared."""
    opportunity.preparable(conn, [rec.id])
    cv = store.create(conn, "cv", now, _name(rec.name, "CV"), rec.id, [b.to_api() for b in blocks])
    portfolio = store.create(conn, "portfolio", now, _name(rec.name, "Portfolio"), rec.id, [])
    opportunity.set_prepared(conn, rec.id, cv.id)
    return cv.id, portfolio.id
