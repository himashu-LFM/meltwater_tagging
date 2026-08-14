"""
Bentley article fetcher — reads the full text of a news article from its URL.

Two layers, deliberately separated (see the "general fetcher" design):
  1. RETRIEVE  — get the raw HTML (anonymous httpx; a CDP/real-Chrome path can
                 be added later for paywalled / bot-walled sites).
  2. EXTRACT   — turn raw HTML into clean article text (trafilatura, which strips
                 nav/ads/boilerplate and returns the main body).

It routes by URL: reddit.com would use a Reddit parser (Kaseya's job); every
other site uses the general article extractor. For Bentley all sources are
news sites, so the general path is what runs.
"""

import httpx
import trafilatura

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Max characters of article text to keep (mirrors config.MAX_POST_CHARS intent).
MAX_CHARS = 12000


def _retrieve(url: str, timeout: float = 20.0) -> tuple[str, int]:
    """Fetch raw HTML anonymously. Returns (html, status_code)."""
    with httpx.Client(headers=_HEADERS, follow_redirects=True, timeout=timeout) as client:
        resp = client.get(url)
        return resp.text, resp.status_code


def _extract(html: str, url: str) -> str:
    """Extract the main article text from raw HTML."""
    text = trafilatura.extract(
        html,
        url=url,
        include_comments=False,
        include_tables=False,
        favor_precision=True,
    )
    return (text or "").strip()


def fetch_article(url: str) -> dict:
    """Fetch + extract one article.

    Returns a dict:
      { url, ok, text, chars, status, error }
    `ok` is True only when we got usable article text. Callers should treat
    ok=False as "could not read" (paywalled / blocked / empty) and let the
    classifier decide with whatever metadata exists, or flag for review.
    """
    out = {"url": url, "ok": False, "text": "", "chars": 0, "status": None, "error": None}
    try:
        html, status = _retrieve(url)
        out["status"] = status
        if status >= 400 or not html:
            out["error"] = f"HTTP {status}"
            return out
        text = _extract(html, url)
        if not text:
            out["error"] = "no extractable article text (possible paywall / JS-only page)"
            return out
        if len(text) > MAX_CHARS:
            text = text[:MAX_CHARS]
        out["text"] = text
        out["chars"] = len(text)
        out["ok"] = True
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    return out
