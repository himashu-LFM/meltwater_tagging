"""Run the FULL fetch_article in an isolated, killable process.

Some news pages make a native step (trafilatura/lxml parse) OR a regex pass over
the raw HTML spin at 100% CPU. In-thread that is un-killable and holds the GIL,
which freezes every other worker. Running the whole fetch in its own process lets
the caller KILL it on a hard timeout, so no page can freeze the batch.

Usage:  python -m brands.bentley.fetch_worker <url> <out_json>
Writes the fetch_article() result dict as JSON to out_json.
"""

import json
import os
import sys

# The whole worker is already isolated + killable, so parse in-process here (no
# nested sub-subprocess) — the caller's timeout bounds everything.
os.environ["MELTWATER_PARSE_SUBPROCESS"] = "false"

PROJ = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if PROJ not in sys.path:
    sys.path.insert(0, PROJ)

from brands.bentley.fetcher import fetch_article


def main() -> None:
    url = sys.argv[1]
    out = sys.argv[2]
    try:
        r = fetch_article(url)
    except Exception as e:
        r = {"url": url, "ok": False, "text": "", "chars": 0, "status": None,
             "error": f"{type(e).__name__}: {e}", "via": None,
             "summary_only": False, "author": "", "syndicated_stub": False}
    try:
        with open(out, "w", encoding="utf-8") as f:
            f.write(json.dumps(r))
    except Exception:
        pass


if __name__ == "__main__":
    main()
