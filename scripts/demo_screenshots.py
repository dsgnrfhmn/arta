#!/usr/bin/env python3
"""Screenshots of the demo for the README.

    python scripts/demo_screenshots.py                  # into docs/screenshots/
    python scripts/demo_screenshots.py --out /some/dir

Starts the demo on a fresh temporary data folder (the demo you run yourself is not
touched), opens the pages in headless Chromium at 1920 x 1080 and saves one picture per
view, as WebP (lossless for the interface, quality 90 where pictures fill the frame).
The demo data and pictures are deterministic, so the screenshots change only when the
interface does. Needs Playwright's Chromium (`python -m playwright install chromium`,
done by scripts/install_macos.sh).
"""

from __future__ import annotations

import argparse
import io
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image  # noqa: E402

from core.config import REPO_ROOT  # noqa: E402
from core.db import Database  # noqa: E402
from demo import __main__ as demo  # noqa: E402
from sets import store  # noqa: E402
from web import server  # noqa: E402

VIEWPORT = {"width": 1920, "height": 1080}
SCALE = 1                                  # every screenshot is exactly 1920 x 1080
PORTFOLIO_SET = "Textile Art Prize"        # opens with the woven tapestries
CV_SET = "Textile CV"

# Only pictures on screen: the pages load the ones further down lazily, as they scroll in.
VISIBLE_PICTURES_LOADED = """[...document.images].filter(i => {
  const r = i.getBoundingClientRect();
  return r.width > 0 && r.bottom > 0 && r.top < innerHeight && r.right > 0 && r.left < innerWidth;
}).every(i => i.complete && i.naturalWidth > 0)"""


def set_id(db: Database, kind: str, name: str) -> str:
    with db.read() as c:
        return next(r.id for r in store.list_sets(c, kind) if r.name == name)


def settle(page) -> None:
    """Wait until the page has loaded its data and the pictures on screen. Polled from here: the
    pages' CSP allows no eval, which page.wait_for_function would need."""
    page.wait_for_load_state("networkidle")
    for _ in range(100):
        if page.evaluate(VISIBLE_PICTURES_LOADED):
            break
        page.wait_for_timeout(100)
    else:
        raise TimeoutError(f"pictures still loading on {page.url}")
    page.wait_for_timeout(600)


def save(data: bytes, path: Path, pictures: bool) -> None:
    """WebP: lossless for an interface shot, quality 90 for one full of pictures."""
    img = Image.open(io.BytesIO(data)).convert("RGB")
    img.save(path, "WEBP", method=6, **({"quality": 90} if pictures else {"lossless": True}))
    print(f"  {path.relative_to(REPO_ROOT) if REPO_ROOT in path.parents else path}  "
          f"{path.stat().st_size // 1024} KB")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, default=REPO_ROOT / "docs" / "screenshots")
    args = parser.parse_args()
    from playwright.sync_api import sync_playwright

    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="arta-shots-") as tmp_name:
        tmp = Path(tmp_name)
        cfg = demo.demo_config(tmp / "data", None)
        demo.prepare(tmp / "data", reset=False)
        demo.fill(cfg)
        db = Database(cfg.db_path)
        srv = server.make_server(cfg, db, port=0, **server.load_runtime(cfg))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        cv = f"/cv.html?set={set_id(db, 'cv', CV_SET)}"
        portfolio = f"/portfolio.html?set={set_id(db, 'portfolio', PORTFOLIO_SET)}"
        shots = [   # file, page, theme, whether pictures fill the frame
            ("for-you.webp", "/for-you.html", "light", False),
            ("for-you-dark.webp", "/for-you.html", "dark", False),
            ("cv.webp", cv, "light", False),
            ("cv-dark.webp", cv, "dark", False),
            ("portfolio.webp", portfolio, "light", True),
            ("portfolio-dark.webp", portfolio, "dark", True),
        ]
        print(f"arta screenshots -> {out}")
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch()
                for name, path, theme, pictures in shots:
                    context = browser.new_context(viewport=VIEWPORT, device_scale_factor=SCALE, reduced_motion="reduce")
                    context.add_init_script(f"try {{ localStorage.setItem('ds-theme', '{theme}') }} catch (e) {{}}")
                    page = context.new_page()
                    page.goto(base + path)
                    settle(page)
                    save(page.screenshot(), out / name, pictures)
                    context.close()
                browser.close()
        finally:
            srv.shutdown()
            srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
