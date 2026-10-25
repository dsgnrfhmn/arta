"""The ProjectSource interface.

One interface, two implementations: the shared Drive folder (projects.source = "drive")
and a folder on this machine (LocalProjects: "local", or "fixtures" for the synthetic
test tree). Callers never see paths:
projects and images are addressed by opaque ids taken from a listing, so a
request can never name a file the source did not list.
"""

from __future__ import annotations

from typing import Protocol

from models.project import Project

# Refuse to load an original larger than this into memory.
MAX_ORIGINAL_BYTES = 200 * 1024 * 1024


class SourceError(Exception):
    """The source cannot answer (unreadable, too large). The message is user-facing."""


class NotFound(LookupError):
    """No such project or image in the source."""


class ProjectSource(Protocol):
    def list_projects(self) -> list[Project]:
        """Every project, sorted by name. Never raises for one broken project."""

    def get_project(self, project_id: str) -> Project:
        """Raises NotFound."""

    def fingerprint(self, project_id: str, image_id: str) -> str:
        """Changes whenever the original changes, without reading its content.
        The thumbnail cache keys on it, so a stale entry is refreshed, never trusted."""

    def read_image(self, project_id: str, image_id: str) -> bytes:
        """The original's bytes (lazy, never cached). Raises NotFound or SourceError."""
