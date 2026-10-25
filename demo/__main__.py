"""python -m demo: the dashboard on made-up data, offline, at http://127.0.0.1:8455/.

    python -m demo                  start; the first start draws the pictures and fills the database
    python -m demo --reset          delete the demo's data and start again from the beginning
    python -m demo --port 8460      use another port
    python -m demo --data-dir DIR   keep the demo's data in DIR instead of the default folder

Nothing real is read: the configuration is demo/config.toml (every source "demo", the
mock Claude client, no Google account), never ~/.config/arta. The demo's data (database,
caches, the drawn project folders) lives outside the repository in one folder that carries
a .arta-demo mark; edits made in the demo stay there between starts, and --reset deletes
only a folder with that mark.
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import sqlite3
import sys
import tomllib
from pathlib import Path

from core import config as config_mod
from core.config import REPO_ROOT, Config, ConfigError
from core.db import Database, DbError
from core.seed import seed_cv_sets, seed_opportunities, seed_portfolio_sets, seed_updates
from demo.build import build_projects

DEMO_DIR = REPO_ROOT / "demo"
MARK = ".arta-demo"


def default_data_dir(env: "os._Environ[str] | dict[str, str]" = os.environ, platform: str = sys.platform) -> Path:
    """Where the demo keeps its data unless --data-dir says otherwise: the per-user data
    folder of the platform, never the repository."""
    home = Path(env.get("HOME") or Path.home())
    if platform == "darwin":
        return home / "Library" / "Application Support" / "arta-demo"
    xdg = env.get("XDG_DATA_HOME")
    return (Path(xdg) if xdg else home / ".local" / "share") / "arta-demo"


def prepare(data_dir: Path, reset: bool) -> None:
    """The demo's folder, created and marked; with reset, deleted first. A folder that
    exists without the mark is not the demo's and is never touched."""
    marked = (data_dir / MARK).is_file()
    if data_dir.exists() and not marked and any(data_dir.iterdir()):
        raise ConfigError(f"{data_dir} is not empty and not a demo folder (no {MARK}). Choose another --data-dir.")
    if reset and marked:
        shutil.rmtree(data_dir)
    data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    (data_dir / MARK).write_text("Data of `python -m demo`. Safe to delete.\n", "utf-8")


def demo_config(data_dir: Path, port: int | None) -> Config:
    """demo/config.toml with the data folder (and a port) filled in, checked like any
    configuration, then checked to be the demo: nothing live, nothing real."""
    with (DEMO_DIR / "config.toml").open("rb") as f:
        data = tomllib.load(f)
    data["server"]["data_dir"] = str(data_dir)
    if port is not None:
        data["server"]["port"] = port
    cfg = config_mod.parse(data)
    demo_only = (cfg.intake == ("demo",) and cfg.updates == "demo" and cfg.projects == "demo" and cfg.cv == "demo"
                 and cfg.llm == "mock" and not cfg.uses_google and cfg.api_key_file is None)
    if not demo_only:
        raise ConfigError("demo/config.toml must use only the demo sources and the mock client.")
    return cfg


def fill(cfg: Config) -> dict[str, int]:
    """Draw the project folders if they are missing, then seed every empty table. A table
    that already has rows is left as the demo's user left it."""
    from web import server   # after the configuration is known: it imports every source

    drawn = 0
    if not cfg.demo_projects_dir.exists():
        print("arta demo: drawing the demo pictures (a few seconds, once)...", file=sys.stderr)
        drawn = build_projects(DEMO_DIR / "projects.json", cfg.demo_projects_dir)
    db = Database(cfg.db_path)
    db.migrate()
    return {
        "pictures": drawn,
        "opportunities": seed_opportunities(db, cfg.demo_dir),
        "updates": seed_updates(db, cfg.demo_dir),
        "cv_sets": seed_cv_sets(db, cfg.demo_dir, server.build_cv(cfg)),
        "portfolio_sets": seed_portfolio_sets(db, cfg.demo_dir, server.build_projects(cfg)),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m demo", description="arta on made-up data, offline.")
    parser.add_argument("--reset", action="store_true", help="delete the demo's data and start from the beginning")
    parser.add_argument("--port", type=int, help="port on 127.0.0.1 (default 8455)")
    parser.add_argument("--data-dir", type=Path, help="folder for the demo's data (default: the per-user data folder)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    data_dir = (args.data_dir or default_data_dir()).expanduser().resolve()
    try:
        cfg = demo_config(data_dir, args.port)     # checks the folder is outside the repository
        prepare(data_dir, args.reset)
        filled = fill(cfg)
    except (ConfigError, DbError, sqlite3.Error, ValueError, OSError) as e:
        print(f"arta demo: cannot start: {e}", file=sys.stderr)
        return 2
    seeded = ", ".join(f"{n} {k}" for k, n in filled.items() if n)
    print(f"arta demo: http://127.0.0.1:{cfg.port}/  (data in {data_dir}{'; added ' + seeded if seeded else ''})\n"
          f"arta demo: `python -m demo --reset` starts again from the beginning.", file=sys.stderr)

    from web import server
    return server.serve(cfg)


if __name__ == "__main__":
    sys.exit(main())
