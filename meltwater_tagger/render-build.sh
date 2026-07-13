#!/usr/bin/env bash
# Render build command — installs Python deps AND the Chromium browser that
# Playwright needs for the "Apply to Meltwater" automation.
#
# Use "python -m playwright" (not the bare "playwright" CLI) so the install
# always targets the same Python environment gunicorn will run under — a bare
# "playwright" on PATH can resolve to a different interpreter/venv on Render's
# build image, which downloads browsers into a cache the running app never
# looks at (this is the #1 cause of "Executable doesn't exist" at runtime).
set -e
pip install -r requirements.txt
python -m playwright install --with-deps chromium
