"""
Standalone Playwright renderer — runs in its OWN process (its own main thread),
so it's Windows-safe even when the webapp calls it from a request worker thread.

Usage (spawned by fetcher._retrieve_playwright_subprocess):
    python -m brands.bentley.playwright_fetch <url>

Prints the rendered HTML to stdout (capped). The parent captures stdout with a
timeout; if this process hangs, the parent kills it and flags the item for review.
Kept deliberately tiny and dependency-light so it starts fast.
"""

import sys

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
_MAX = 800000  # cap stdout so a huge page doesn't flood the pipe


def main() -> int:
    if len(sys.argv) < 2:
        return 2
    url = sys.argv[1]
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

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.stdout.write(html[:_MAX])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
