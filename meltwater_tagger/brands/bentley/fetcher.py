"""
Bentley article fetcher — reads the full text of a news article from its URL.

Strategy (general, not per-site), tried until we get usable text:
  1. httpx + trafilatura body            — most static news sites (fast).
  2. FOLLOW-TO-SOURCE                     — if the page is an aggregator/summary
     (body not extractable) and it points to the ORIGINAL article on another
     domain (via canonical / og:url / a link sharing the same slug), fetch that
     instead. This is how a human/agent reaches the real story behind a
     ground.news-style aggregator link.
  3. Playwright (headless Chromium) body  — JS-heavy sites.
  4. meta fallback                        — <title>/og:title + og:description;
     the headline+summary nearly every site sets. Marked `summary_only`.

Also extracts the AUTHOR (meta / JSON-LD / byline) generally, so the classifier
can decide Unique-vs-Press-release even when the byline isn't in the body.

If nothing usable is found, ok=False and the caller flags for review.
"""

import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
from html import unescape
from urllib.parse import urlparse

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

MAX_CHARS = 24000  # long earnings-call transcripts name products/people late; don't truncate them out
_BOILER = ("privacy policy", "cookie policy", "we value your privacy",
           "enable javascript", "consent to", "accept all cookies",
           "this website utilizes", "opens in a new window")


# --- extraction helpers ------------------------------------------------------

def _extract_body(html: str, url: str) -> str:
    # trafilatura parsing a huge DOM (heavy JS sites can be 1MB+) is slow; the
    # article body is near the top, so cap the HTML we parse to stay fast.
    text = trafilatura.extract(
        html[:600000], url=url, include_comments=False, include_tables=False, favor_precision=True,
    )
    return (text or "").strip()


def _body_ok(text: str) -> bool:
    if not text:
        return False
    low = text.lower()
    if len(text) < 300 or (len(text) < 900 and any(m in low for m in _BOILER)):
        return False
    return True


def _meta(html: str, prop: str, attr: str) -> str:
    for pat in (
        r'<meta[^>]*%s=["\']%s["\'][^>]*content=["\'](.*?)["\']' % (attr, re.escape(prop)),
        r'<meta[^>]*content=["\'](.*?)["\'][^>]*%s=["\']%s["\']' % (attr, re.escape(prop)),
    ):
        m = re.search(pat, html, re.I | re.S)
        if m:
            return unescape(m.group(1)).strip()
    return ""


def _extract_meta(html: str) -> str:
    """Fallback text: headline + summary from social-share meta tags."""
    title = ""
    mt = re.search(r"<title>(.*?)</title>", html, re.I | re.S)
    if mt:
        title = unescape(mt.group(1)).strip()
    headline = _meta(html, "og:title", "property") or title
    desc = _meta(html, "og:description", "property") or _meta(html, "description", "name")
    return "\n\n".join(p for p in (headline, desc) if p)


def _extract_author(html: str) -> str:
    """Best-effort author/byline from meta tags or JSON-LD (general)."""
    a = _meta(html, "author", "name") or _meta(html, "article:author", "property")
    if a and len(a) < 80:
        return a
    for pat in (r'"author"\s*:\s*\{[^}]*?"name"\s*:\s*"([^"]{2,80})"',
                r'"author"\s*:\s*"([^"]{2,80})"'):
        m = re.search(pat, html)
        if m:
            return unescape(m.group(1)).strip()
    return ""


def _host(u: str) -> str:
    return urlparse(u).netloc.lower().replace("www.", "")


def _find_source_link(html: str, base_url: str) -> str:
    """If this page points to the ORIGINAL article on another domain, return it.
    Uses canonical / og:url first, then an outbound link sharing the URL slug."""
    base = _host(base_url)
    segs = [s for s in urlparse(base_url).path.split("/") if s]
    slug = segs[-1] if segs else ""

    for u in (_meta(html, "og:url", "property"),):
        if u and _host(u) and _host(u) != base:
            return u
    canon = re.search(r'<link[^>]*rel=["\']canonical["\'][^>]*href=["\'](.*?)["\']', html, re.I)
    if canon and _host(canon.group(1)) and _host(canon.group(1)) != base:
        return canon.group(1)
    if len(slug) >= 12:  # avoid matching on trivial slugs
        for m in re.finditer(r'href=["\'](https?://[^"\']+)["\']', html, re.I):
            u = m.group(1)
            if _host(u) and _host(u) != base and slug.lower() in u.lower():
                return u
    return None


# --- retrieval ---------------------------------------------------------------

def _retrieve_httpx(url: str, timeout: float = 20.0) -> tuple[str, int]:
    with httpx.Client(headers=_HEADERS, follow_redirects=True, timeout=timeout) as client:
        resp = client.get(url)
        return resp.text, resp.status_code


def _retrieve_playwright(url: str, timeout_ms: int = 15000) -> str:
    """Direct sync Playwright — only safe in the MAIN thread (used by the CLI)."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page(user_agent=_HEADERS["User-Agent"])
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            page.wait_for_timeout(1500)
            return page.content()
        finally:
            browser.close()


def _kill_process_tree(proc) -> None:
    """Kill the child AND all its descendants (Playwright's Chromium and its
    helper processes). Without this, killing only the Python child leaves
    Chromium alive holding resources — the root cause of the old hang."""
    try:
        if os.name == "nt":
            # /T kills the whole tree; /F forces it.
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True)
        else:
            # The child was started in its own session (setsid), so its pgid ==
            # its pid and every descendant shares it — one killpg takes them all.
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _retrieve_playwright_subprocess(url: str, timeout_s: int = 25) -> str:
    """Render via a separate Python process (its own main thread) so it works
    from a webapp request worker thread WITHOUT hanging. Windows-/thread-safe.

    Two things make the timeout reliable (the old version hung despite a
    timeout):
      1. The child writes the HTML to a TEMP FILE, not stdout — so Chromium can't
         keep an output PIPE open and block the parent's read.
      2. The child runs in its OWN process group; on timeout we kill the whole
         tree (Chromium included), not just the Python child.
    Returns '' on timeout/failure."""
    proj = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    fd, tmp = tempfile.mkstemp(prefix="mw_pw_", suffix=".html")
    os.close(fd)
    popen_kwargs = {}
    if os.name == "nt":
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_kwargs["start_new_session"] = True  # setsid -> own process group
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "brands.bentley.playwright_fetch", url, tmp],
            cwd=proj, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            **popen_kwargs,
        )
    except Exception:
        _safe_unlink(tmp)
        return ""
    try:
        proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        _kill_process_tree(proc)
        try:
            proc.wait(timeout=5)
        except Exception:
            pass
        _safe_unlink(tmp)
        return ""  # timed out -> no HTML (item gets flagged for review)
    except Exception:
        _kill_process_tree(proc)
        _safe_unlink(tmp)
        return ""
    # process finished on its own -> read whatever HTML it wrote
    html = ""
    try:
        with open(tmp, encoding="utf-8", errors="replace") as f:
            html = f.read()
    except Exception:
        html = ""
    finally:
        _safe_unlink(tmp)
    return html


def _safe_unlink(path: str) -> None:
    try:
        os.remove(path)
    except Exception:
        pass


def fetch_article(url: str, _depth: int = 0) -> dict:
    """Fetch + extract one article. Returns:
      { url, ok, text, chars, status, error, via, summary_only, author }
    `_depth` guards the follow-to-source recursion (max 1 hop).
    """
    out = {"url": url, "ok": False, "text": "", "chars": 0, "status": None,
           "error": None, "via": None, "summary_only": False, "author": ""}

    def _finish(text, source, kind, html):
        out["text"] = text[:MAX_CHARS]
        out["chars"] = len(out["text"])
        out["ok"] = True
        out["via"] = f"{source}-{kind}"
        out["summary_only"] = (kind == "meta")
        out["author"] = _extract_author(html)

    httpx_html = None
    # 1) httpx body
    try:
        httpx_html, status = _retrieve_httpx(url)
        out["status"] = status
        if status < 400 and httpx_html:
            body = _extract_body(httpx_html, url)
            if _body_ok(body):
                _finish(body, "httpx", "body", httpx_html)
                return out
    except Exception as e:
        out["error"] = f"httpx: {type(e).__name__}: {e}"

    # 2) follow aggregator -> original source (one hop)
    if _depth == 0 and httpx_html:
        src = _find_source_link(httpx_html, url)
        if src and src.rstrip("/") != url.rstrip("/"):
            deep = fetch_article(src, _depth=1)
            if deep["ok"] and not deep["summary_only"]:   # got a real body at source
                deep["via"] = (deep["via"] or "") + "+followed-source"
                deep["url"] = url                          # report the input URL
                return deep

    # 3) Playwright body (JS-heavy pages).
    #  - MAIN thread (CLI): direct sync Playwright (fast, Windows-safe here).
    #  - WORKER thread (webapp request): a separate subprocess renderer — sync
    #    Playwright hangs in worker threads on Windows, so we run it in its own
    #    process (its own main thread) with a hard timeout.
    #  ON by default now: the subprocess renderer writes to a temp file (no held
    #  pipe) and its timeout kills the whole process tree (Chromium included), so
    #  a stuck child can no longer block the caller. Set
    #  MELTWATER_PLAYWRIGHT_SUBPROCESS=false to fall back to skip->review.
    pw_html = None
    try:
        if threading.current_thread() is threading.main_thread():
            pw_html = _retrieve_playwright(url)
        elif os.environ.get("MELTWATER_PLAYWRIGHT_SUBPROCESS", "true").lower() == "true":
            pw_html = _retrieve_playwright_subprocess(url)
        if pw_html:
            body = _extract_body(pw_html, url)
            if _body_ok(body):
                _finish(body, "playwright", "body", pw_html)
                return out
    except Exception as e:
        if not out["error"]:
            out["error"] = f"playwright: {type(e).__name__}: {e}"

    # 4) meta fallback (summary) — from whichever HTML we have
    for src, html in (("playwright", pw_html), ("httpx", httpx_html)):
        if html:
            meta = _extract_meta(html)
            if len(meta) >= 80:
                _finish(meta, src, "meta", html)
                return out

    if not out["error"]:
        out["error"] = "no usable text extracted"
    return out
