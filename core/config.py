"""Runtime configuration, read once from one TOML file outside the repository.

Where: $ARTA_CONFIG (an absolute path), else ~/.config/arta/config.toml. Nothing
else is read: no environment variables for settings, no fallback file.

Fail loud: the file has exactly the keys of config.example.toml. A missing key, an
unknown key (a typo) or an invalid value stops startup with a message that names the
key. Optional values are written as "" or [], never left out, so there are no silent
defaults. Every new setting is added to config.example.toml in the same change; a test
parses the example, so the two cannot drift apart.

Secrets are not in this file. It holds paths to them (llm.api_key_file,
google.token_file), each a separate file, mode 0600, outside the repository; their
content is read where it is used and never kept in Config.

The Python service always binds loopback (HOST); there is deliberately no setting for it.
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

REPO_ROOT = Path(__file__).resolve().parent.parent

CONFIG_VAR = "ARTA_CONFIG"
EXAMPLE_NAME = "config.example.toml"

# Loopback only: the dashboard is opened from a bookmark on the same machine.
HOST = "127.0.0.1"

LLM_MODES = ("mock", "live")
# Each kind of content names its own source; nothing follows anything else. "none" means
# not configured (the page says so), "fixtures" is the synthetic test data in tests/fixtures/,
# "demo" the made-up demo data in demo/ (`python -m demo`).
# "Load opportunities" reads every intake source in the listed order: "gmail" is the
# dedicated account (read-only), "artconnect" the public listing at artconnect.com.
INTAKES = ("gmail", "artconnect", "fixtures", "demo")
UPDATE_SOURCES = ("gmail", "fixtures", "demo", "none")
PROJECT_SOURCES = ("drive", "local", "fixtures", "demo", "none")
CV_SOURCES = ("drive", "local", "fixtures", "demo", "none")

_DRIVE_ID = re.compile(r"[A-Za-z0-9_-]{10,200}")

# Every key of the file and the TOML type its value must have. config.example.toml
# lists exactly these.
SCHEMA: dict[str, dict[str, type | tuple[type, ...]]] = {
    "server": {"port": int, "data_dir": str},
    "intake": {"sources": list},
    "updates": {"source": str},
    "projects": {"source": str, "local_dir": str, "drive_folder": str, "drive_categories": list,
                 "registry_file": str},
    "cv": {"source": str, "local_file": str, "drive_file": str},
    "portfolio_cover": {"drive_file": str, "file": str, "text_file": str},
    "llm": {"mode": str, "model": str, "api_key_file": str, "artist_profile_file": str,
            "max_usd": (int, float), "max_calls": int, "cache": bool},
    "google": {"token_file": str, "client_secret_file": str},
}


class ConfigError(Exception):
    """Configuration is missing or invalid. The message is user-facing."""


@dataclass(frozen=True)
class Config:
    llm: str
    port: int
    data_dir: Path
    # llm.model: the model to call with llm.mode = "live". There is no default.
    model: str | None = None
    # intake.sources: names from INTAKES in reading order, or None when none is configured.
    intake: tuple[str, ...] | None = None
    # updates.source, projects.source, cv.source: one of their *_SOURCES, None for "none".
    updates: str | None = None
    projects: str | None = None
    cv: str | None = None
    # projects.local_dir: the artist's project folder on this machine (projects = "local").
    projects_local_dir: Path | None = None
    # projects.drive_folder / .drive_categories (projects = "drive"): the folder shared to
    # the arta account, and the subfolders of it whose project folders are read; no
    # categories: the project folders sit directly inside drive_folder.
    drive_folder: str | None = None
    drive_categories: tuple[str, ...] = ()
    # projects.registry_file: authors and places with their spellings
    # (sources/image_meta.Registry). It names real people. None: names read by position.
    registry_file: Path | None = None
    # cv.local_file (cv = "local"): a master CV file written by scripts/build_cv_master.py.
    # cv.drive_file (cv = "drive"): the Drive file id of the master CV, a Google Doc shared
    # to the arta account, read through sources/drive_cv.py with a cache.
    cv_local_file: Path | None = None
    cv_drive_file: str | None = None
    # portfolio_cover.drive_file / .file: the artist's portfolio PDF on Drive or on disk,
    # whose page 1 opens every portfolio set (sources/portfolio_cover.py). At most one.
    cover_drive_file: str | None = None
    cover_file: Path | None = None
    # portfolio_cover.text_file: the cover's words as JSON; with it the cover is rebuilt
    # as text around the PDF's photo, without it it is the page image.
    cover_text: Path | None = None
    # llm.max_usd / llm.max_calls: a live client refuses the call that would pass either.
    # llm.cache = false forces fresh calls.
    llm_max_usd: float = 1.0
    llm_max_calls: int = 20
    llm_cache: bool = True
    # llm.artist_profile_file: the artist profile (core/artist_profile.py). Required live.
    profile_file: Path | None = None
    # llm.api_key_file: a file holding only the Anthropic API key. Required live.
    api_key_file: Path | None = None
    # google.token_file: the dedicated account's token.json. Required as soon as any
    # source reads Gmail or Drive. google.client_secret_file: only for the consent script.
    google_token_file: Path | None = None
    google_client_secret_file: Path | None = None

    @property
    def uses_google(self) -> bool:
        """True when any configured source reads the dedicated Google account."""
        return bool(
            (self.intake and "gmail" in self.intake) or self.updates == "gmail"
            or self.projects == "drive" or self.cv == "drive" or self.cover_drive_file
        )

    @property
    def llm_cache_path(self) -> Path:
        """Answers of live analysis calls, keyed by content. Outside the repo tree."""
        return self.data_dir / "cache" / "llm_cache.sqlite3"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "arta.sqlite3"

    @property
    def thumb_dir(self) -> Path:
        return self.data_dir / "cache" / "thumbs"

    @property
    def exports_dir(self) -> Path:
        """Finished PDF exports until they are downloaded (web/exports.py); never kept long."""
        return self.data_dir / "exports"

    @property
    def cover_cache_dir(self) -> Path:
        """Page 1 of the portfolio PDF as a JPEG (sources/portfolio_cover.py). Outside the repo tree."""
        return self.data_dir / "cache" / "cover"

    @property
    def cv_cache_path(self) -> Path:
        """The last import of the master CV: blocks with ids. Outside the repo tree."""
        return self.data_dir / "cache" / "cv_master.json"

    @property
    def intake_filters_path(self) -> Path:
        """Pre-filter rules for intake (pipeline/intake_filter.py). Data, versioned with the code."""
        return REPO_ROOT / "config" / "intake_filters.json"

    @property
    def fixtures_dir(self) -> Path:
        return REPO_ROOT / "tests" / "fixtures"

    @property
    def fixture_projects_dir(self) -> Path:
        """The synthetic fixture project tree. Read only when projects == "fixtures"."""
        return self.fixtures_dir / "projects"

    @property
    def fixture_cv_file(self) -> Path:
        """The synthetic fixture master CV. Read only when cv == "fixtures"."""
        return self.fixtures_dir / "cv" / "master.json"

    @property
    def demo_dir(self) -> Path:
        """The demo's data in the repository (demo/): texts, records, the CV. Made up."""
        return REPO_ROOT / "demo"

    @property
    def demo_projects_dir(self) -> Path:
        """The demo's project folders, pictures included, drawn by demo/build.py into the
        data directory (never into the repository)."""
        return self.data_dir / "demo-projects"

    @property
    def drive_projects_dir(self) -> Path:
        """The Drive project listing and texts (never originals). Outside the repo tree."""
        return self.data_dir / "cache" / "drive-projects"


def example_data() -> dict[str, Any]:
    """config.example.toml, parsed: the full set of keys with their example values."""
    with (REPO_ROOT / EXAMPLE_NAME).open("rb") as f:
        return tomllib.load(f)


def config_path(env: Mapping[str, str] | None = None) -> Path:
    """The file load() reads: $ARTA_CONFIG, else ~/.config/arta/config.toml."""
    env = os.environ if env is None else env
    raw = env.get(CONFIG_VAR, "").strip()
    if raw:
        path = Path(raw)
        if not path.is_absolute():
            raise ConfigError(f"{CONFIG_VAR}={raw!r} must be an absolute path.")
        return path
    home = env.get("HOME", "").strip()
    if not home:
        raise ConfigError(f"{CONFIG_VAR} is not set and HOME is unavailable to find ~/.config/arta/config.toml.")
    return Path(home) / ".config" / "arta" / "config.toml"


def load(path: Path | None = None, repo_root: Path = REPO_ROOT) -> Config:
    """Read and check the configuration file (config_path() when path is None)."""
    path = config_path() if path is None else path
    if not path.is_file():
        raise ConfigError(
            f"No configuration at {path}. Copy {EXAMPLE_NAME} from the repository there, "
            f"chmod 600 it, and fill it in."
        )
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path} is not valid TOML: {e}") from None
    except OSError as e:
        raise ConfigError(f"{path} could not be read: {e.strerror}.") from None
    return parse(data, repo_root)


def _check_keys(data: Mapping[str, Any]) -> None:
    unknown = sorted(set(data) - set(SCHEMA))
    if unknown:
        raise ConfigError(f"Unknown section(s): {', '.join(unknown)}. Compare with {EXAMPLE_NAME}.")
    for section, keys in SCHEMA.items():
        table = data.get(section)
        if not isinstance(table, dict):
            raise ConfigError(f"Section [{section}] is missing. Compare with {EXAMPLE_NAME}.")
        missing = [k for k in keys if k not in table]
        if missing:
            raise ConfigError(
                f"[{section}] is missing {', '.join(missing)}. Every key must be present "
                f'("" or [] for not set); compare with {EXAMPLE_NAME}.'
            )
        extra = sorted(set(table) - set(keys))
        if extra:
            raise ConfigError(f"[{section}] has unknown key(s): {', '.join(extra)}. Compare with {EXAMPLE_NAME}.")
        for key, kind in keys.items():
            value = table[key]
            # bool is an int in Python; a port or a count written as true is still wrong.
            if not isinstance(value, kind) or (isinstance(value, bool) and kind is not bool):
                raise ConfigError(f"{section}.{key} has the wrong type ({type(value).__name__}).")


def _path(name: str, raw: str, repo_root: Path, outside_repo: bool = False) -> Path | None:
    raw = raw.strip()
    if not raw:
        return None
    path = Path(raw)
    if not path.is_absolute():
        raise ConfigError(f"{name}={raw!r} must be an absolute path.")
    if outside_repo:
        resolved, root = path.resolve(), repo_root.resolve()
        if resolved == root or root in resolved.parents:
            raise ConfigError(f"{name}={raw!r} is inside the repository tree. Choose a path outside it.")
    return path


def _drive_id(name: str, raw: str) -> str | None:
    raw = raw.strip()
    if raw and not _DRIVE_ID.fullmatch(raw):
        raise ConfigError(f"{name} is not a Drive file id (letters, digits, - and _ only).")
    return raw or None


def _choice(name: str, raw: str, valid: tuple[str, ...]) -> str | None:
    """One of valid; "none" becomes None. Empty is an error: there is no default."""
    value = raw.strip()
    if value not in valid:
        raise ConfigError(f"{name}={raw!r} is invalid. Valid: {', '.join(valid)}.")
    return None if value == "none" else value


def _only_for(section: str, chosen: str | None, needs: dict[str, tuple[str, Any]]) -> None:
    """Each source's own setting: required when that source is chosen, and refused when
    another one is, because a value that has no effect would only look configured."""
    for source, (key, value) in needs.items():
        if chosen == source and value is None:
            raise ConfigError(f'{section}.source = "{source}" needs {section}.{key}.')
        if chosen != source and value is not None:
            raise ConfigError(f'{section}.{key} is set but {section}.source is not "{source}". Clear it.')


def parse(data: Mapping[str, Any], repo_root: Path = REPO_ROOT) -> Config:
    """Check a parsed configuration and build Config. load() reads the file; tests call this."""
    _check_keys(data)
    server, cover, llm_t, google = data["server"], data["portfolio_cover"], data["llm"], data["google"]

    port = server["port"]
    if not 1 <= port <= 65535:
        raise ConfigError(f"server.port={port} is out of range 1-65535.")
    # The database and caches hold unpublished work and application state. They must
    # never land where `git add` could pick them up.
    data_dir = _path("server.data_dir", server["data_dir"], repo_root, outside_repo=True)
    if data_dir is None:
        raise ConfigError("server.data_dir is not set. There is no default; choose a directory outside the repository.")

    names = data["intake"]["sources"]
    if any(not isinstance(n, str) or n not in INTAKES for n in names) or len(set(names)) != len(names):
        raise ConfigError(
            f"intake.sources={names!r} is invalid. Valid: a list of {', '.join(INTAKES)}, each once, or [] for none."
        )
    intake = tuple(names) or None

    updates = _choice("updates.source", data["updates"]["source"], UPDATE_SOURCES)

    pt = data["projects"]
    projects = _choice("projects.source", pt["source"], PROJECT_SOURCES)
    projects_local_dir = _path("projects.local_dir", pt["local_dir"], repo_root, outside_repo=True)
    drive_folder = pt["drive_folder"].strip() or None
    categories = pt["drive_categories"]
    if any(not isinstance(c, str) or not c.strip() for c in categories) or len(set(categories)) != len(categories):
        raise ConfigError("projects.drive_categories must be a list of folder names, each once, or [].")
    _only_for("projects", projects, {"local": ("local_dir", projects_local_dir),
                                     "drive": ("drive_folder", drive_folder)})
    if categories and projects != "drive":
        raise ConfigError('projects.drive_categories is set but projects.source is not "drive". Clear it.')
    registry_file = _path("projects.registry_file", pt["registry_file"], repo_root, outside_repo=True)

    ct = data["cv"]
    cv = _choice("cv.source", ct["source"], CV_SOURCES)
    cv_local_file = _path("cv.local_file", ct["local_file"], repo_root, outside_repo=True)
    cv_drive_file = _drive_id("cv.drive_file", ct["drive_file"])
    _only_for("cv", cv, {"local": ("local_file", cv_local_file), "drive": ("drive_file", cv_drive_file)})

    cover_drive_file = _drive_id("portfolio_cover.drive_file", cover["drive_file"])
    cover_file = _path("portfolio_cover.file", cover["file"], repo_root)
    if cover_drive_file is not None and cover_file is not None:
        raise ConfigError("portfolio_cover.drive_file and portfolio_cover.file are both set. Choose one cover source.")
    cover_text = _path("portfolio_cover.text_file", cover["text_file"], repo_root)

    llm = llm_t["mode"].strip()
    if llm not in LLM_MODES:
        raise ConfigError(f"llm.mode={llm!r} is invalid. Valid: {', '.join(LLM_MODES)}.")
    model = llm_t["model"].strip() or None
    api_key_file = _path("llm.api_key_file", llm_t["api_key_file"], repo_root, outside_repo=True)
    profile_file = _path("llm.artist_profile_file", llm_t["artist_profile_file"], repo_root)
    if llm == "live":
        if api_key_file is None:
            raise ConfigError("llm.mode = \"live\" needs llm.api_key_file. It does not fall back to mock.")
        if model is None:
            raise ConfigError("llm.mode = \"live\" needs llm.model (the model name). There is no default.")
        if profile_file is None:
            raise ConfigError(
                "llm.mode = \"live\" needs llm.artist_profile_file. "
                "Without it the analysis would have nothing to judge eligibility against."
            )
    max_usd = float(llm_t["max_usd"])
    if not 0 < max_usd <= 1000:
        raise ConfigError(f"llm.max_usd={max_usd} is out of range (above 0, at most 1000).")
    max_calls = llm_t["max_calls"]
    if not 1 <= max_calls <= 10_000:
        raise ConfigError(f"llm.max_calls={max_calls} is out of range 1-10000.")

    token_file = _path("google.token_file", google["token_file"], repo_root, outside_repo=True)
    secret_file = _path("google.client_secret_file", google["client_secret_file"], repo_root, outside_repo=True)

    cfg = Config(
        llm=llm, port=port, data_dir=data_dir, model=model, intake=intake, updates=updates,
        projects=projects, projects_local_dir=projects_local_dir, drive_folder=drive_folder,
        drive_categories=tuple(c.strip() for c in categories), registry_file=registry_file,
        cv=cv, cv_local_file=cv_local_file, cv_drive_file=cv_drive_file,
        cover_drive_file=cover_drive_file, cover_file=cover_file, cover_text=cover_text,
        llm_max_usd=max_usd, llm_max_calls=max_calls, llm_cache=llm_t["cache"],
        profile_file=profile_file, api_key_file=api_key_file,
        google_token_file=token_file, google_client_secret_file=secret_file,
    )
    if cfg.uses_google and token_file is None:
        raise ConfigError("A source reads Gmail or Drive, so google.token_file is required.")
    return cfg


def read_secret(name: str, path: Path) -> str:
    """The content of a one-value secret file, stripped. Refuses a file that group or
    other can read. The message names the path, never the content."""
    try:
        mode = path.stat().st_mode
    except FileNotFoundError:
        raise ConfigError(f"{name}: {path} does not exist.") from None
    if mode & 0o077:
        raise ConfigError(f"{name}: {path} is accessible to other users. Run: chmod 600 {path}")
    try:
        value = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        raise ConfigError(f"{name}: {path} could not be read as UTF-8 text.") from None
    if not value:
        raise ConfigError(f"{name}: {path} is empty.")
    return value
