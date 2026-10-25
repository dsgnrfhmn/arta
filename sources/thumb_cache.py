"""Thumbnail cache.

Holds thumbnails only (about 800 px JPEG). HEIC is converted here. Originals
are read lazily, once per (image, fingerprint), and are never stored. Nothing
is uploaded anywhere: decoding and resizing happen in this process.

Invalidation rule: the file name carries a hash of the source's fingerprint,
so a changed original maps to a new file and the old one is deleted. The
source wins on conflict; a stale entry is refreshed, never trusted. Serving a
cached thumbnail costs one fingerprint lookup (a stat, or Drive metadata),
not a read of the original.

Failing loud: an image that cannot be decoded raises ThumbError with the
reason, and HEIC without the decoder says so instead of returning nothing.
"""

from __future__ import annotations

import hashlib
import io
import os
import tempfile
import warnings
from pathlib import Path

from PIL import Image, ImageOps

from sources.project_source import ProjectSource

try:  # HEIC decoder. Absent in a bare environment; then HEIC fails loudly.
    import pillow_heif

    pillow_heif.register_heif_opener()
    HEIC_SUPPORTED = True
except ImportError:  # pragma: no cover - depends on the environment
    HEIC_SUPPORTED = False

MAX_PX = 800
QUALITY = 82
PRINT_QUALITY = 90


class ThumbError(Exception):
    """The thumbnail cannot be made. The message is user-facing."""


def print_image(data: bytes, max_px: int) -> bytes:
    """An original as a JPEG at most max_px on its long side, for a PDF export
    (web/pdf_export.py). Made in memory and never stored, like every original."""
    return _make(data, max_px, quality=PRINT_QUALITY)


def _make(data: bytes, max_px: int, quality: int = QUALITY) -> bytes:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as im:
                im = ImageOps.exif_transpose(im)
                im = im.convert("RGB")
                im.thumbnail((max_px, max_px))
                out = io.BytesIO()
                im.save(out, "JPEG", quality=quality, optimize=True)
                return out.getvalue()
    except Image.UnidentifiedImageError:
        if data[4:8] == b"ftyp" and not HEIC_SUPPORTED:
            raise ThumbError("HEIC images need pillow-heif, which is not installed.") from None
        raise ThumbError("The image could not be decoded.") from None
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise ThumbError("The image is too large to decode safely.") from None
    except OSError:
        raise ThumbError("The image is damaged or unreadable.") from None


class ThumbCache:
    def __init__(self, directory: Path, max_px: int = MAX_PX):
        self.dir = Path(directory)
        self.max_px = max_px

    def _prepare(self) -> None:
        # Owner-only: the thumbnails are unpublished work.
        self.dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.dir, 0o700)

    @staticmethod
    def _stem(project_id: str, image_id: str) -> str:
        return f"{project_id}-{image_id}"

    def etag(self, source: ProjectSource, project_id: str, image_id: str) -> str:
        return hashlib.sha256(source.fingerprint(project_id, image_id).encode()).hexdigest()[:16]

    def get(self, source: ProjectSource, project_id: str, image_id: str) -> Path:
        """Path of an up-to-date thumbnail, made if missing or stale."""
        tag = self.etag(source, project_id, image_id)     # raises NotFound
        stem = self._stem(project_id, image_id)
        target = self.dir / f"{stem}-{tag}.jpg"
        if target.is_file():
            return target
        self._prepare()
        thumb = _make(source.read_image(project_id, image_id), self.max_px)
        fd, tmp = tempfile.mkstemp(dir=self.dir, prefix=".tmp-", suffix=".jpg")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(thumb)
            os.chmod(tmp, 0o600)
            os.replace(tmp, target)      # atomic: a reader never sees half a file
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        for old in self.dir.glob(f"{stem}-*.jpg"):
            if old != target:
                old.unlink(missing_ok=True)
        return target
