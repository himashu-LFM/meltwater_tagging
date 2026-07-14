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
# NOTE: we do NOT use "--with-deps" — that shells out to apt-get via su/sudo,
# which Render's native (non-root) build environment refuses ("su:
# Authentication failure"), aborting the whole build. We install only the
# browser binary (no root needed). Render's base image is expected to already
# provide Chromium's shared libraries; the non-fatal check at the end reports
# if any are missing so we can see it in the build log without breaking deploy.
#
# Use "python -m playwright" (not the bare "playwright" CLI) so the install
# always targets the same Python environment gunicorn will run under.
set -euo pipefail

pip install -r requirements.txt

# Browser-only install (no system deps, no root).
python -m playwright install chromium

# Non-fatal launch check: prints the resolved path and whether headless Chromium
# actually starts. Never fails the build — so classification stays deployed even
# if apply's browser can't launch, and the build log shows exactly what's wrong
# (e.g. "error while loading shared libraries: libnss3.so").
python - <<'PY' || true
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    print("Playwright chromium executable:", p.chromium.executable_path, flush=True)
    try:
        b = p.chromium.launch(headless=True)
        b.close()
        print("OK: headless Chromium launches — apply should work.", flush=True)
    except Exception as e:
        print("WARNING: Chromium is installed but did NOT launch on this image.",
              flush=True)
        print("  -> apply will fail until the missing system libraries are "
              "provided (consider the Docker/Playwright-image deploy).", flush=True)
        print(f"  -> details: {e}", flush=True)
PY
