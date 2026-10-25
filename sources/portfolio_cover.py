"""The portfolio's cover: the first page of the artist's own portfolio PDF.

dsgnrfhmn, 2026-10-24: every portfolio set opens with the cover of the PDF (her photo and
her details), as the PDF does. The page is not rebuilt from data: it is the PDF's
page 1, rendered to a JPEG with poppler's pdftoppm and drawn on a full sheet.

Two sources, one shape (`current()` -> Cover):
  DriveCover  the PDF on Drive (portfolio_cover.drive_file), read-only. Drive is
              the source of truth: at most every `ttl` seconds its md5 is asked, a
              changed md5 means download and render again. If Drive cannot be asked
              the call fails loudly with SourceError; a cached cover is never served
              as if it were live (same rule as sources/drive_cv.py).
  FileCover   a local PDF (portfolio_cover.file), for a deployment without Drive.

The rendered page lives in the data dir (cache/cover/<fingerprint>.jpg, 0600), never in
the repo tree, and is only served to the dashboard; nothing is uploaded anywhere.

Cover as text (dsgnrfhmn, 2026-10-24): page 1 of this PDF has no text layer (its words are
outlines), so its words were transcribed once into a JSON file kept outside the repo
(portfolio_cover.text_file, see load_cover_text) and the page is rebuilt from it. The
photo is still read from the PDF: the largest image embedded in page 1, extracted as it
is stored (pdfimages -j, no re-encoding) and cached as <fingerprint>-photo.jpg.
"""

from __future__ import annotations

import hashlib
import io
import os
import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sources.project_source import SourceError

PDF_MIME = "application/pdf"
DOWNLOAD_MAX = 60_000_000
# Twice the sheet's 842 pt width: sharp on a 2x screen, about 300 KB as a JPEG.
RENDER_WIDTH = 1684
RENDER_TIMEOUT = 60


@dataclass(frozen=True)
class Cover:
    fingerprint: str
    path: Path
    photo: Path | None = None   # the largest image of page 1, when it has one


COVER_TEXT_FIELDS = {"title": str, "name": str, "details": list, "heading": str, "paragraphs": list, "contacts": list}


def load_cover_text(path: Path) -> dict[str, Any]:
    """The cover's words: {title, name, details[], heading, paragraphs[], contacts[]}.
    Raises SourceError naming what is wrong; the text is never guessed."""
    import json
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise SourceError("The cover text file could not be read as JSON.") from None
    if not isinstance(data, dict) or set(data) != set(COVER_TEXT_FIELDS):
        raise SourceError(f"The cover text file must have exactly: {', '.join(COVER_TEXT_FIELDS)}.")
    for key, kind in COVER_TEXT_FIELDS.items():
        value = data[key]
        if not isinstance(value, kind) or (kind is list and not all(isinstance(v, str) for v in value)):
            raise SourceError(f"The cover text file's {key} must be {'a list of texts' if kind is list else 'a text'}.")
    return data


def extract_photo(pdf: bytes, out: Path) -> bool:
    """The largest image embedded in page 1, as stored, at `out`. False when it has none."""
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "in.pdf"
        src.write_bytes(pdf)
        try:
            subprocess.run(["pdfimages", "-f", "1", "-l", "1", "-j", str(src), str(Path(tmp) / "img")],
                           check=True, capture_output=True, timeout=RENDER_TIMEOUT)
        except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            return False
        found = sorted((p for p in Path(tmp).glob("img-*") if p.suffix in (".jpg", ".png", ".ppm")),
                       key=lambda p: p.stat().st_size, reverse=True)
        if not found:
            return False
        data = found[0].read_bytes()
        if found[0].suffix != ".jpg":
            from PIL import Image
            buf = io.BytesIO()
            Image.open(io.BytesIO(data)).convert("RGB").save(buf, "JPEG", quality=92)
            data = buf.getvalue()
        _write_private(out, data)
        return True


def render_first_page(pdf: bytes, out: Path) -> None:
    """Page 1 of `pdf` as a JPEG at `out`. Raises SourceError when it cannot be drawn."""
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "in.pdf"
        src.write_bytes(pdf)
        try:
            subprocess.run(
                ["pdftoppm", "-f", "1", "-l", "1", "-singlefile", "-jpeg", "-scale-to-x", str(RENDER_WIDTH),
                 "-scale-to-y", "-1", str(src), str(Path(tmp) / "page")],
                check=True, capture_output=True, timeout=RENDER_TIMEOUT,
            )
        except FileNotFoundError:
            raise SourceError("The cover cannot be drawn: pdftoppm (poppler-utils) is not installed.") from None
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            raise SourceError("The cover cannot be drawn: page 1 of the PDF did not render.") from None
        _write_private(out, (Path(tmp) / "page.jpg").read_bytes())


def _write_private(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".cover-")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _keep_only(cache_dir: Path, keep: Path) -> None:
    for old in cache_dir.iterdir():
        if old.is_file() and not old.name.startswith(keep.stem) and not old.name.startswith(".cover-"):
            old.unlink(missing_ok=True)


def _ensure(pdf_of: Callable[[], bytes], cache_dir: Path, fingerprint: str) -> Cover:
    out = cache_dir / f"{fingerprint}.jpg"
    photo = cache_dir / f"{fingerprint}-photo.jpg"
    no_photo = cache_dir / f"{fingerprint}-nophoto"      # page 1 was looked at: it has no image
    if not out.exists() or not (photo.exists() or no_photo.exists()):
        pdf = pdf_of()
        if not out.exists():
            render_first_page(pdf, out)
        if not extract_photo(pdf, photo):
            _write_private(no_photo, b"")
        _keep_only(cache_dir, out)
    return Cover(fingerprint, out, photo if photo.exists() else None)


class FileCover:
    def __init__(self, pdf_path: Path, cache_dir: Path):
        self.pdf_path = Path(pdf_path)
        self.cache_dir = Path(cache_dir)

    def current(self) -> Cover:
        try:
            pdf = self.pdf_path.read_bytes()
        except OSError:
            raise SourceError("The cover PDF could not be read.") from None
        fingerprint = hashlib.sha256(pdf).hexdigest()[:32]
        return _ensure(lambda: pdf, self.cache_dir, fingerprint)


class DriveCover:
    def __init__(
        self,
        file_id: str,
        cache_dir: Path,
        service: Callable[[], Any],
        ttl: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.file_id = file_id
        self.cache_dir = Path(cache_dir)
        self._service = service
        self.ttl = ttl
        self._clock = clock
        self._checked_at: float | None = None
        self._cover: Cover | None = None

    def _drive(self):
        return self._service()

    def current(self) -> Cover:
        now = self._clock()
        if self._cover is not None and self._checked_at is not None and now - self._checked_at < self.ttl:
            return self._cover
        try:
            svc = self._drive()
            meta = svc.files().get(fileId=self.file_id, fields="id,mimeType,md5Checksum,size",
                                   supportsAllDrives=True).execute()
            if meta.get("mimeType") != PDF_MIME or not meta.get("md5Checksum"):
                raise SourceError("The cover file on Drive is not a PDF.")
            if int(meta.get("size") or 0) > DOWNLOAD_MAX:
                raise SourceError("The cover PDF on Drive is larger than expected.")
            cover = _ensure(lambda: self._download(svc), self.cache_dir, meta["md5Checksum"])
        except SourceError:
            raise
        except Exception as exc:  # google client, auth and network errors: no detail that could hold a token
            raise SourceError(f"Drive could not be read ({type(exc).__name__}). Nothing stale is served.") from None
        self._cover, self._checked_at = cover, now
        return self._cover

    def _download(self, svc) -> bytes:
        from googleapiclient.http import MediaIoBaseDownload
        buf = io.BytesIO()
        downloader = MediaIoBaseDownload(buf, svc.files().get_media(fileId=self.file_id, supportsAllDrives=True),
                                         chunksize=8 * 1024 * 1024)
        done = False
        while not done:
            _, done = downloader.next_chunk()
            if buf.tell() > DOWNLOAD_MAX:
                raise SourceError("The cover PDF on Drive is larger than expected.")
        return buf.getvalue()
