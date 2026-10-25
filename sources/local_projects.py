"""ProjectSource over a project folder on this machine: projects.source = "local"
(projects.local_dir, the artist's own folder) or "fixtures" (tests/fixtures/projects/,
synthetic test data). One folder per project directly inside the root.

Layout it copes with (the real folder, not an idealised one): images in the
project root and in exhibition subfolders, mixed extensions, files whose name
lies about their type, text as one docx per language, any of it missing.

Ids are hashes of relative paths. Clients only ever hold ids from a listing,
and a path is looked up from an id, never built from input. Symlinks are
never followed: a link out of the tree would read files that are not project
content.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from core import sanitize
from models.project import ImageRef, Project, UnsupportedFile
from sources import image_meta, project_files as pf
from sources.project_source import MAX_ORIGINAL_BYTES, NotFound, SourceError

NAME_MAX = 255
TEXT_MAX = 50_000


def _id(*parts: str) -> str:
    return hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()[:16]


def _clean_name(raw: str) -> str:
    try:
        return sanitize.plain_text(raw, NAME_MAX) or "(unnamed)"
    except ValueError:
        return "(name too long)"


def _head(path: Path, n: int = 16) -> bytes:
    with open(path, "rb") as f:
        return f.read(n)


class LocalProjects:
    def __init__(self, root: Path, registry: image_meta.Registry | None = None):
        self.root = Path(root)
        self.registry = registry

    # -- listing ---------------------------------------------------------

    def _project_dirs(self) -> list[Path]:
        # A missing folder is a wrong path in the configuration, not an empty portfolio.
        if not self.root.is_dir():
            raise SourceError(f"The project folder {self.root} does not exist or is not a folder.")
        return sorted(
            (p for p in self.root.iterdir() if p.is_dir() and not p.is_symlink() and not p.name.startswith(".")),
            key=lambda p: pf.natural_key(p.name),
        )

    def _files(self, project_dir: Path) -> list[tuple[str | None, Path]]:
        """(group, path): root files first, then each subfolder in turn, natural
        order, to any depth. group is the folder path below the project."""
        out: list[tuple[str | None, Path]] = []

        def walk(group, directory):
            entries = sorted(directory.iterdir(), key=lambda p: pf.natural_key(p.name))
            for p in entries:
                if not p.name.startswith(".") and (p.is_file() or p.is_symlink()):
                    out.append((group, p))
            for sub in entries:
                if sub.is_dir() and not sub.is_symlink() and not sub.name.startswith("."):
                    walk(f"{group}/{sub.name}" if group else sub.name, sub)

        walk(None, project_dir)
        return out

    def _build(self, project_dir: Path) -> Project:
        pid = _id(project_dir.name)
        images: list[ImageRef] = []
        unsupported: list[UnsupportedFile] = []
        texts: dict[str, tuple] = {}
        for group, path in self._files(project_dir):
            name = _clean_name(path.name)
            label_group = _clean_name(group) if group else None
            if path.is_symlink() or not path.is_file():
                unsupported.append(UnsupportedFile(name, label_group, 0, "symbolic link, not followed"))
                continue
            size = path.stat().st_size
            try:
                head = _head(path)
            except OSError:
                unsupported.append(UnsupportedFile(name, label_group, size, "unreadable"))
                continue
            kind, reason = pf.sniff(head)
            if kind:
                rel = f"{group or ''}/{path.name}"
                meta = image_meta.parse_image_meta(name, tuple(group.split("/")) if group else (), self.registry)
                width, height = pf.image_size(path, kind)
                images.append(ImageRef(_id(project_dir.name, rel), name, label_group, kind, size,
                                       meta.role, _clean_name(meta.author), _clean_name(meta.place) if meta.place else "", meta.year,
                                       width, height))
            elif path.suffix.lower() == ".docx" and pf.is_zip(head):
                try:
                    text = sanitize.plain_text(pf.docx_text(path), TEXT_MAX)
                except ValueError as e:
                    unsupported.append(UnsupportedFile(name, label_group, size, f"text not usable: {e}"))
                    continue
                lang = pf.language_of(text)
                depth = group.count("/") + 1 if group else 0
                kept = texts.get(lang)
                note = f"other {lang} text, not used (kept the shallowest)"
                if kept is None or depth < kept[0]:
                    if kept is not None:
                        unsupported.append(UnsupportedFile(*kept[2], note))
                    texts[lang] = (depth, text, (name, label_group, size))
                else:
                    unsupported.append(UnsupportedFile(name, label_group, size, note))
            else:
                unsupported.append(UnsupportedFile(name, label_group, size, reason))
        return Project(pid, _clean_name(project_dir.name), tuple(images), {lang: t[1] for lang, t in texts.items()}, tuple(unsupported))

    def list_projects(self) -> list[Project]:
        return [self._build(d) for d in self._project_dirs()]

    def _dir_for(self, project_id: str) -> Path:
        for d in self._project_dirs():
            if _id(d.name) == project_id:
                return d
        raise NotFound(f"No such project: {project_id}")

    def get_project(self, project_id: str) -> Project:
        return self._build(self._dir_for(project_id))

    # -- originals -------------------------------------------------------

    def _path_for(self, project_id: str, image_id: str) -> Path:
        project_dir = self._dir_for(project_id)
        for group, path in self._files(project_dir):
            if path.is_symlink() or not path.is_file():
                continue
            rel = f"{group or ''}/{path.name}"
            if _id(project_dir.name, rel) == image_id and pf.sniff(_head(path))[0]:
                return path
        raise NotFound(f"No such image: {image_id}")

    def fingerprint(self, project_id: str, image_id: str) -> str:
        st = self._path_for(project_id, image_id).stat()
        return f"{st.st_mtime_ns}-{st.st_size}"

    def read_image(self, project_id: str, image_id: str) -> bytes:
        path = self._path_for(project_id, image_id)
        if path.stat().st_size > MAX_ORIGINAL_BYTES:
            raise SourceError("The original is too large to load.")
        try:
            return path.read_bytes()
        except OSError as e:
            raise SourceError(f"Could not read the original: {e.strerror or 'I/O error'}.") from None
