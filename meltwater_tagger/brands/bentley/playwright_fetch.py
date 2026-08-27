"""
Standalone Playwright renderer — runs in its OWN process (its own main thread),
so it's Windows-safe even when the webapp calls it from a request worker thread.

Usage (spawned by fetcher._retrieve_playwright_subprocess):
    python -m brands.bentley.playwright_fetch <url> [<outfile>]

Writes the rendered HTML to <outfile> if given, else to stdout. Writing to a
FILE is what makes the parent's timeout reliable: Playwright's Chromium
grandchild otherwise keeps the stdout PIPE open, so the parent blocks reading it
even after the timeout fires. With a file, there is no pipe to hold — the parent
just kills the whole process group and reads whatever the file has.
Kept deliberately tiny and dependency-light so it starts fast.
"""

import sys

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
_MAX = 800000  # cap output so a huge page doesn't blow up memory/disk


def main() -> int:
    if len(sys.argv) < 2:
        return 2
    url = sys.argv[1]
    outfile = sys.argv[2] if len(sys.argv) > 2 else None
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page(user_agent=_UA)
                page.goto(url, wait_until="domcontentloaded", timeout=20000)
                page.wait_for_timeout(1500)
                html = page.content()
            finally:
                browser.close()
    except Exception as e:
        sys.stderr.write(f"{type(e).__name__}: {e}")
        return 1

    html = html[:_MAX]
    if outfile:
        with open(outfile, "w", encoding="utf-8", errors="replace") as f:
            f.write(html)
    else:
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
        sys.stdout.write(html)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
