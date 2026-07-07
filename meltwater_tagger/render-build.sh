#!/usr/bin/env bash
# Render build command — installs Python deps AND the Chromium browser that
# Playwright needs for the "Apply to Meltwater" automation.
set -e
pip install -r requirements.txt
playwright install --with-deps chromium
