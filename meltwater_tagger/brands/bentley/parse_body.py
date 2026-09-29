"""Run trafilatura extraction in an ISOLATED process so a pathological/huge DOM
that makes lxml/trafilatura spin at 100% CPU can be KILLED on a timeout instead
of hanging the whole batch (native code can't be interrupted in-thread, and it
holds the GIL, starving every other worker).

Usage:  python -m brands.bentley.parse_body <html_in> <text_out> [url]

Reads HTML from html_in, writes the extracted body text to text_out. Uses the
SAME extract parameters as fetcher._extract_body, so on a normal page the output
is byte-identical to the in-process path (no effect on tagging). Kept minimal —
imports only trafilatura — so the per-parse process starts in ~0.3s.
"""

import sys


def main() -> None:
    html_in = sys.argv[1]
    text_out = sys.argv[2]
    url = sys.argv[3] if len(sys.argv) > 3 else ""
    try:
        with open(html_in, encoding="utf-8", errors="replace") as f:
            html = f.read()
    except Exception:
        html = ""
    text = ""
    try:
        import trafilatura
        # identical params + 600k cap as fetcher._extract_body
        text = trafilatura.extract(
            html[:600000], url=url or None, include_comments=False,
            include_tables=False, favor_precision=True,
        ) or ""
    except Exception:
        text = ""
    try:
        with open(text_out, "w", encoding="utf-8") as f:
            f.write(text.strip())
    except Exception:
        pass


if __name__ == "__main__":
    main()
