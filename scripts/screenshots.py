#!/usr/bin/env python3
"""Regenerate the dashboard screenshots in docs/images/ from synthetic data — no
car needed. Spawns `serve --fake`, drives a headless browser through the three
views (Live with charts, Configuration, Codes) and writes the PNGs.

Requires Playwright and a Chromium (or system Chrome):

    pip install playwright
    playwright install chromium      # or rely on an installed Google Chrome

    python scripts/screenshots.py

The page renders in dark theme at 2x for a crisp image.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "images"
PORT = 8099
# A DPF/engine spread that fills a 3x3 grid and shows moving charts.
SELECTED = ["rpm", "boost", "boost_desired", "maf", "egt", "dpfp", "rail", "egr", "clt"]


def _wait_up(url: str, tries: int = 40) -> bool:
    for _ in range(tries):
        try:
            urllib.request.urlopen(url, timeout=1)
            return True
        except Exception:  # noqa: BLE001
            time.sleep(0.25)
    return False


def main() -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("pip install playwright  (and: playwright install chromium)", file=sys.stderr)
        return 2

    OUT.mkdir(parents=True, exist_ok=True)
    url = f"http://127.0.0.1:{PORT}/"
    serve = subprocess.Popen(
        [sys.executable, "-m", "volvo_diag", "serve", "--fake",
         "--host", "127.0.0.1", "--port", str(PORT)],
        cwd=ROOT, env={**os.environ, "PYTHONPATH": str(ROOT / "python")},
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        if not _wait_up(url):
            print("serve --fake did not come up", file=sys.stderr)
            return 1
        with sync_playwright() as p:
            # channel="chrome" uses an installed Google Chrome; drop it to use the
            # Playwright-managed Chromium from `playwright install chromium`.
            try:
                browser = p.chromium.launch(channel="chrome", headless=True)
            except Exception:  # noqa: BLE001
                browser = p.chromium.launch(headless=True)
            ctx = browser.new_context(viewport={"width": 1440, "height": 900},
                                      device_scale_factor=2, color_scheme="dark")
            ctx.add_init_script(
                "try{localStorage.setItem('volvo.sel.hs',%s);}catch(e){}"
                % json.dumps(json.dumps(SELECTED)))
            page = ctx.new_page()
            page.goto(url, wait_until="load")  # the page polls forever; never idle
            page.wait_for_timeout(13000)       # let the charts draw a wave
            page.screenshot(path=str(OUT / "dashboard.png"))
            page.click("#tab-dtc")
            page.wait_for_timeout(1500)
            page.screenshot(path=str(OUT / "dashboard-codes.png"))
            page.click("#tab-config")
            page.wait_for_timeout(1500)
            page.screenshot(path=str(OUT / "dashboard-config.png"))
            browser.close()
        print(f"wrote dashboard.png, dashboard-codes.png, dashboard-config.png to {OUT}")
        return 0
    finally:
        serve.terminate()
        try:
            serve.wait(timeout=5)
        except subprocess.TimeoutExpired:
            serve.kill()


if __name__ == "__main__":
    raise SystemExit(main())
