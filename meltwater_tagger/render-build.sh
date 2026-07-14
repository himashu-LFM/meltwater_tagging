#!/usr/bin/env bash
# Render build command — installs Python deps AND the Chromium browser that
# Playwright needs for the "Apply to Meltwater" automation.
#
# IMPORTANT: this script only runs if Render's "Build Command" is set to invoke
# it (e.g.  bash render-build.sh  — adjust the path to match your Root Directory).
# If the Build Command is left at the default "pip install -r requirements.txt",
# the browser is never installed and every /api/apply fails at runtime with
# "Executable doesn't exist".
#
# Use "python -m playwright" (not the bare "playwright" CLI) so the install
# always targets the same Python environment gunicorn will run under — a bare
# "playwright" on PATH can resolve to a different interpreter/venv on Render's
# build image, which downloads browsers into a cache the running app never
# looks at (this is the #1 cause of "Executable doesn't exist" at runtime).
set -euo pipefail

pip install -r requirements.txt

# Install the Chromium build Playwright needs. Prefer WITH system deps (works
# when the build runs as root), but fall back to the browser-only install if
# that fails — Render's native builds aren't root and can't apt-get, and a
# failing --with-deps must NOT abort the whole build and leave us with no
# browser at all. The Render base image already ships the common shared libs.
python -m playwright install --with-deps chromium \
  || python -m playwright install chromium

# Verify the browser can ACTUALLY launch headless (exactly what apply does), so
# a broken/mismatched install fails the BUILD loudly and early instead of every
# apply request at runtime. Prints the resolved path for debugging.
python - <<'PY'
import sys
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    print("Playwright chromium executable:", p.chromium.executable_path, flush=True)
    try:
        browser = p.chromium.launch(headless=True)
        browser.close()
    except Exception as e:
        sys.exit(f"FATAL: Chromium did not launch after install: {e}")
print("OK: headless Chromium is installed and launches.", flush=True)
PY
