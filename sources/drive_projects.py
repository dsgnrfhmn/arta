"""ProjectSource over the shared Drive folder, with a local cache.

Drive layout: <folder>/<category>/<project>/... , or <folder>/<project>/... when no
categories are configured (projects.drive_folder, projects.drive_categories). <folder> is
the one folder of that name shared to the arta account. Only the configured categories
are read, and the category level is dropped: every project of them is one project here.

What is cached (data_dir/cache/drive-projects/state.json): the listing (every file's
id, name, folder, type, version and pixel size) and the projects' texts. Originals are
never stored; read_image fetches one from Drive when a thumbnail is made, and the
thumbnail cache keys on the file's Drive version (fingerprint), so an edited original
gets a new thumbnail.

Refresh rule (dsgnrfhmn, 2026-10-22): the cache is refreshed only when refresh() is called,
which the Portfolio page does when Add project or Create new set is pressed. Between
refreshes nothing asks Drive, except read_image for a thumbnail not made yet. The first
use with no cache refreshes once. A refresh that fails raises SourceError and leaves the
previous listing in place; the page says so and shows the date of that listing.

Ids: a project's id is the hash of its folder name (as in the fixture tree, so sets made
on the fixtures keep their projects); an image's id is its Drive file id, so renaming a
file on Drive keeps it picked in a set.

Read-only: Drive read scope, no write.
"""

from __future__ import annotations

import io
import json
import os
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from core import sanitize
from models.project import ImageRef, Project, UnsupportedFile
from sources import image_meta, project_files as pf
from sources.local_projects import TEXT_MAX, _clean_name, _id
from sources.project_source import MAX_ORIGINAL_BYTES, NotFound, SourceError

FOLDER = "application/vnd.google-apps.folder"
GOOGLE_DOC = "application/vnd.google-apps.document"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
TEXT_DOWNLOAD_MAX = 10_000_000
STATE_VERSION = 1
_KINDS = {"image/jpeg": "jpeg", "image/png": "png", "image/webp": "webp", "image/heic": "heic", "image/heif": "heic"}
_FIELDS = "nextPageToken,files(id,name,mimeType,parents,md5Checksum,modifiedTime,size,imageMediaMetadata(width,height,rotation))"


def _tidy(name: str) -> str:
    return " ".join(name.split())


def _quoted(value: str) -> str:
    """A value for a Drive query string literal: backslash and quote escaped, so a folder
    name from the configuration cannot end the literal and add query terms."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


class DriveProjects:
    def __init__(
        self,
        cache_dir: Path,
        registry: image_meta.Registry | None,
        service: Callable[[], Any],
        folder: str,
        categories: tuple[str, ...] = (),
        clock: Callable[[], float] = time.time,
    ):
        if not folder.strip():
            raise ValueError("DriveProjects needs the name of the shared folder.")
        self.cache_dir = Path(cache_dir)
        self.registry = registry
        self._service = service
        self.folder = folder
        self.categories = tuple(categories)
        self._clock = clock
        self._lock = threading.Lock()
        self._state: dict | None = None
        self._state_stamp: tuple | None = None   # of the state file _state was read from
        self._projects: dict[str, Project] | None = None

    # -- ProjectSource -------------------------------------------------------

    def list_projects(self) -> list[Project]:
        return sorted(self._built().values(), key=lambda p: pf.natural_key(p.name))

    def get_project(self, project_id: str) -> Project:
        project = self._built().get(project_id)
        if project is None:
            raise NotFound(f"No such project: {project_id}")
        return project

    def fingerprint(self, project_id: str, image_id: str) -> str:
        f = self._file(project_id, image_id)
        return f"{f['id']}-{f['version']}"

    def read_image(self, project_id: str, image_id: str) -> bytes:
        f = self._file(project_id, image_id)
        if f["size"] > MAX_ORIGINAL_BYTES:
            raise SourceError("The original is too large to load.")
        try:
            return self._download(self._drive().files().get_media(fileId=f["id"]), MAX_ORIGINAL_BYTES)
        except SourceError:
            raise
        except Exception as exc:  # google client, auth and network errors: no detail that could hold a token
            raise SourceError(f"Drive could not be read ({type(exc).__name__}).") from None

    # -- refresh ---------------------------------------------------------------

    @property
    def refreshed_at(self) -> float | None:
        state = self._load()
        return state["refreshed_at"] if state else None

    def refresh(self) -> list[Project]:
        """Read the listing and changed texts from Drive, replace the cache. One at a time:
        a second caller waits and gets the fresh result instead of walking Drive again."""
        started = self._clock()
        with self._lock:
            state = self._load()
            if state and state["refreshed_at"] >= started:
                return self.list_projects()
            try:
                new = self._walk(self._drive(), state)
            except SourceError:
                raise
            except Exception as exc:  # google client, auth and network errors
                raise SourceError(f"Drive could not be read ({type(exc).__name__}).{self._as_of(state)}") from None
            self._save(new)
        return self.list_projects()

    @staticmethod
    def _as_of(state: dict | None) -> str:
        if not state:
            return ""
        return " Showing the projects as of " + time.strftime("%Y-%m-%d %H:%M", time.localtime(state["refreshed_at"])) + "."

    # -- Drive -------------------------------------------------------------------

    def _drive(self):
        return self._service()

    @staticmethod
    def _everything(svc) -> dict[str, list[dict]]:
        """Every file the account can see, as parent id -> children. One listing of a few
        pages instead of one per folder (2 s instead of 20 s for the real folder). The
        account sees only what is shared to it (CLAUDE.md, restrict by sharing)."""
        index: dict[str, list[dict]] = {}
        token = None
        while True:
            resp = svc.files().list(q="trashed=false", pageSize=1000, pageToken=token, fields=_FIELDS).execute()
            for f in resp.get("files", []):
                for parent in f.get("parents", []):
                    index.setdefault(parent, []).append(f)
            token = resp.get("nextPageToken")
            if not token:
                return index

    @staticmethod
    def _download(request, limit: int) -> bytes:
        from googleapiclient.http import MediaIoBaseDownload
        buf = io.BytesIO()
        dl = MediaIoBaseDownload(buf, request, chunksize=8 * 1024 * 1024)
        done = False
        while not done:
            _, done = dl.next_chunk()
            if buf.tell() > limit:
                raise SourceError("A file on Drive is larger than expected.")
        return buf.getvalue()

    def _walk(self, svc, old: dict | None) -> dict:
        resp = svc.files().list(
            q=f"name='{_quoted(self.folder)}' and mimeType='{FOLDER}' and sharedWithMe and trashed=false",
            fields="files(id)").execute()
        roots = resp.get("files", [])
        if len(roots) != 1:
            raise SourceError(f"Expected one shared folder named {self.folder!r} on Drive, found {len(roots)}.")
        index = self._everything(svc)
        children = lambda folder_id: index.get(folder_id, [])
        root_id = roots[0]["id"]
        if self.categories:
            found = {f["name"]: f["id"] for f in children(root_id) if f["mimeType"] == FOLDER}
            missing = [c for c in self.categories if c not in found]
            if missing:
                raise SourceError(f"Folder(s) {', '.join(missing)} not found in {self.folder} on Drive.")
            parents = [found[c] for c in self.categories]
        else:
            parents = [root_id]

        known = {f["id"]: f for p in (old or {}).get("projects", []) for f in p["files"]}
        projects, seen = [], set()
        for parent in parents:
            for folder in children(parent):
                if folder["mimeType"] != FOLDER:
                    continue
                name = _tidy(folder["name"])
                if name in seen:     # same name in both categories: one id, keep the first
                    continue
                seen.add(name)
                files: list[dict] = []
                self._collect(svc, children, folder["id"], None, files, known)
                for f in files:
                    if f["text_source"]:
                        self._fill_text(svc, f, known.get(f["id"]))
                projects.append({"name": name, "files": files})
        return {"state_version": STATE_VERSION, "refreshed_at": self._clock(), "projects": projects}

    def _collect(self, svc, children, folder_id: str, group: str | None, out: list[dict], known: dict) -> None:
        """Root files first, then each subfolder in turn, natural order (as LocalProjects)."""
        entries = sorted(children(folder_id), key=lambda f: pf.natural_key(f["name"]))
        for f in entries:
            if f["mimeType"] == FOLDER:
                continue
            meta = f.get("imageMediaMetadata") or {}
            width, height = int(meta.get("width") or 0), int(meta.get("height") or 0)
            if meta.get("rotation") in (1, 3):
                width, height = height, width
            mime = f["mimeType"]
            version = f.get("md5Checksum") or f.get("modifiedTime", "")
            kind = _KINDS.get(mime)
            if kind is None and mime == "application/octet-stream":
                old = known.get(f["id"])
                kind = old["kind"] if old and old["version"] == version else self._sniff(svc, f)
            out.append({
                "id": f["id"], "name": f["name"], "group": group, "mime": mime, "version": version,
                "size": int(f.get("size") or 0), "width": width, "height": height, "kind": kind,
                "text_source": mime == GOOGLE_DOC or (mime == DOCX or f["name"].lower().endswith(".docx")),
            })
        for f in entries:
            if f["mimeType"] == FOLDER:
                self._collect(svc, children, f["id"], f"{group}/{_tidy(f['name'])}" if group else _tidy(f["name"]), out, known)

    @staticmethod
    def _sniff(svc, f: dict) -> str | None:
        """Type of a file Drive does not know (no extension): its first bytes, as the fixture
        tree does. Only these 16 bytes are read."""
        request = svc.files().get_media(fileId=f["id"])
        request.headers["Range"] = "bytes=0-15"
        try:
            return pf.sniff(request.execute())[0]
        except Exception:
            return None

    def _fill_text(self, svc, f: dict, old: dict | None) -> None:
        if old and old["version"] == f["version"]:
            f["text"], f["text_error"] = old.get("text"), old.get("text_error")
            return
        if f["mime"] == GOOGLE_DOC:
            data = svc.files().export(fileId=f["id"], mimeType=DOCX).execute()
        else:
            data = self._download(svc.files().get_media(fileId=f["id"]), TEXT_DOWNLOAD_MAX)
        f["text"], f["text_error"] = None, None
        if len(data) > TEXT_DOWNLOAD_MAX:
            f["text_error"] = "text not usable: document is too large"
            return
        try:
            f["text"] = sanitize.plain_text(pf.docx_text(io.BytesIO(data)), TEXT_MAX)
        except ValueError as e:
            f["text_error"] = f"text not usable: {e}"

    # -- cache -------------------------------------------------------------------

    @property
    def _state_path(self) -> Path:
        return self.cache_dir / "state.json"

    def _load(self) -> dict | None:
        """The cached state, read again when the file changed since: the API's worker
        process (web/jobs.py) has its own instance and must see a refresh made by the
        main process. One stat per call; a missing file keeps what was read."""
        try:
            stamp = self._stamp()
        except FileNotFoundError:
            return self._state
        if self._state is not None and stamp == self._state_stamp:
            return self._state
        try:
            state = json.loads(self._state_path.read_text("utf-8"))
        except FileNotFoundError:
            return self._state
        except (OSError, ValueError):
            raise SourceError(f"The project cache {self._state_path} is unreadable. Delete it to reload from Drive.") from None
        if state.get("state_version") != STATE_VERSION:
            return None
        self._state, self._state_stamp, self._projects = state, stamp, None
        return state

    def _stamp(self) -> tuple:
        # _save replaces the file with a new one, so the inode changes on every save even
        # when two land within one tick of the clock.
        st = self._state_path.stat()
        return st.st_ino, st.st_mtime_ns, st.st_size

    def _save(self, state: dict) -> None:
        self.cache_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.cache_dir, prefix=".state.")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(state, fh, ensure_ascii=False)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self._state_path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        self._state, self._state_stamp, self._projects = state, self._stamp(), None

    def _built(self) -> dict[str, Project]:
        state = self._load()
        if state is None:
            self.refresh()     # first use: there is nothing to show yet
            state = self._load()
        projects = self._projects     # reset by _load when the file changed
        if projects is None:
            projects = {p.id: p for p in (self._build(raw) for raw in state["projects"])}
            self._projects = projects
        return projects

    def _file(self, project_id: str, image_id: str) -> dict:
        self._built()
        for raw in self._state["projects"]:
            if _id(raw["name"]) == project_id:
                for f in raw["files"]:
                    if f["id"] == image_id and f["kind"]:
                        return f
                raise NotFound(f"No such image: {image_id}")
        raise NotFound(f"No such project: {project_id}")

    def _build(self, raw: dict) -> Project:
        images: list[ImageRef] = []
        unsupported: list[UnsupportedFile] = []
        texts: dict[str, tuple] = {}
        for f in raw["files"]:
            name = _clean_name(f["name"])
            group = _clean_name(f["group"]) if f["group"] else None
            if f["kind"]:
                meta = image_meta.parse_image_meta(name, tuple(f["group"].split("/")) if f["group"] else (), self.registry)
                images.append(ImageRef(f["id"], name, group, f["kind"], f["size"], meta.role,
                                       _clean_name(meta.author), _clean_name(meta.place) if meta.place else "", meta.year,
                                       f["width"], f["height"]))
            elif f["text_source"]:
                if f.get("text") is None:
                    unsupported.append(UnsupportedFile(name, group, f["size"], f.get("text_error") or "text not usable"))
                    continue
                lang = pf.language_of(f["text"])
                depth = f["group"].count("/") + 1 if f["group"] else 0
                kept = texts.get(lang)
                note = f"other {lang} text, not used (kept the shallowest)"
                if kept is None or depth < kept[0]:
                    if kept is not None:
                        unsupported.append(UnsupportedFile(*kept[2], note))
                    texts[lang] = (depth, f["text"], (name, group, f["size"]))
                else:
                    unsupported.append(UnsupportedFile(name, group, f["size"], note))
            else:
                reason = "video, not an image" if f["mime"].startswith("video/") else "not a supported image type"
                unsupported.append(UnsupportedFile(name, group, f["size"], reason))
        return Project(_id(raw["name"]), _clean_name(raw["name"]), tuple(images),
                       {lang: t[1] for lang, t in texts.items()}, tuple(unsupported))
