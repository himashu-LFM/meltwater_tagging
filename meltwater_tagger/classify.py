"""
Phase 1 — Classify.

Reads a Meltwater Excel export of the topic feed, fetches full post text where
possible (Reddit permalinks), classifies each post with Claude IN PARALLEL using
the skill's judgment rules, and writes decisions.json.

This is where the speedup comes from: in the skill the agent reads and judges
posts one at a time; here dozens are fetched + classified concurrently, with no
browser in the loop.

Usage:
    python classify.py <export.xlsx> [--brand Kaseya]

If --brand is omitted, the run brand is inferred from the export's topic column.
"""

import argparse
import asyncio
import html as _html
import json
import re
import sys
import time
import xml.etree.ElementTree as _ET

import httpx
import pandas as pd
from anthropic import AsyncAnthropic, AuthenticationError, APIStatusError

import config
from prompts import SYSTEM_PROMPT, POST_TEMPLATE, DECISION_SCHEMA
from results_writer import write_results_excel
from taxonomy import normalize_brand, tag_name, is_valid_tag

# --- Column detection ------------------------------------------------------

PERMALINK_HINTS = ["url", "permalink", "link", "source url", "article url"]
TEXT_HINTS = ["hit sentence", "snippet", "content", "body", "text", "summary", "opening text"]
TOPIC_HINTS = ["search", "topic", "saved search", "query"]
TAG_HINTS = ["tag", "tags"]


def _find_col(df: pd.DataFrame, hints: list[str]) -> str | None:
    lowered = {c.lower().strip(): c for c in df.columns}
    # exact-ish match first
    for h in hints:
        for low, orig in lowered.items():
            if low == h:
                return orig
    # substring match
    for h in hints:
        for low, orig in lowered.items():
            if h in low:
                return orig
    return None


def load_export(path: str) -> tuple[pd.DataFrame, dict]:
    df = pd.read_excel(path)
    cols = {
        "permalink": _find_col(df, PERMALINK_HINTS),
        "text": _find_col(df, TEXT_HINTS),
        "topic": _find_col(df, TOPIC_HINTS),
        "tags": _find_col(df, TAG_HINTS),
    }
    if not cols["permalink"]:
        sys.exit(
            f"Could not find a permalink/URL column. Columns present: {list(df.columns)}\n"
            "Rename the post-URL column to include 'url' or 'permalink', or edit PERMALINK_HINTS."
        )
    return df, cols


def infer_brand(df: pd.DataFrame, topic_col: str | None) -> str | None:
    """Derive the run brand from the topic name, e.g. 'Kaseya V2 | Reddit' -> Kaseya."""
    if not topic_col or topic_col not in df.columns:
        return None
    topics = df[topic_col].dropna().astype(str)
    if topics.empty:
        return None
    raw = topics.mode().iloc[0]
    first = re.split(r"[|\-–]", raw)[0]
    return normalize_brand(first) or normalize_brand(raw)


# --- Full-text fetching ----------------------------------------------------

# Serialize + pace Reddit requests so we don't trip 429s.
_reddit_lock = asyncio.Lock()
_reddit_last = 0.0
_reddit_token: dict = {"value": None, "expires_at": 0.0}


# When Reddit says the budget is spent it also tells us when it refills
# (x-ratelimit-reset). We park every Reddit request until then instead of
# hammering into 429/403s.
_reddit_resume_at = 0.0


async def _throttle():
    global _reddit_last
    async with _reddit_lock:
        loop = asyncio.get_event_loop()
        now = loop.time()
        wait = max(config.REDDIT_MIN_INTERVAL - (now - _reddit_last),
                   _reddit_resume_at - now)
        if wait > 0:
            await asyncio.sleep(wait)
        _reddit_last = loop.time()


def _note_reddit_ratelimit(r) -> None:
    """Read Reddit's rate-limit headers and park the next request until the
    window refills. Anonymous RSS is metered at roughly ONE request per ~25s
    window (x-ratelimit-remaining drops to 0 after a single call), so pacing off
    these headers is the only way a multi-post batch survives."""
    global _reddit_resume_at
    try:
        remaining = float(r.headers.get("x-ratelimit-remaining", "1") or 1)
        reset = float(r.headers.get("x-ratelimit-reset", "0") or 0)
        if remaining <= 0 and reset > 0:
            _reddit_resume_at = asyncio.get_event_loop().time() + reset + 1.0
    except Exception:
        pass


async def _reddit_oauth_token(client: httpx.AsyncClient, force: bool = False) -> str | None:
    """App-only OAuth token (client_credentials) — no user account, no login, no
    CAPTCHA. Cached until shortly before it expires; `force` refetches after a 401.

    This is the supported way to read Reddit programmatically. Anonymous `.json`
    now hits a login/bot wall (403), which is what silently produced empty text
    (and therefore blanket-Neutral classifications)."""
    if not force and _reddit_token["value"]:
        now = asyncio.get_event_loop().time()
        if now < _reddit_token.get("expires_at", 0):
            return _reddit_token["value"]
    if not (config.REDDIT_CLIENT_ID and config.REDDIT_CLIENT_SECRET):
        return None
    try:
        r = await client.post(
            "https://www.reddit.com/api/v1/access_token",
            data={"grant_type": "client_credentials"},
            auth=(config.REDDIT_CLIENT_ID, config.REDDIT_CLIENT_SECRET),
            headers={"User-Agent": config.REDDIT_USER_AGENT},
            timeout=30,
        )
        if r.status_code == 200:
            body = r.json()
            _reddit_token["value"] = body.get("access_token")
            # Refresh a minute early rather than racing the expiry.
            ttl = float(body.get("expires_in", 3600))
            _reddit_token["expires_at"] = asyncio.get_event_loop().time() + max(60.0, ttl - 60)
            return _reddit_token["value"]
        if r.status_code in (401, 403):
            print(f"  Reddit rejected the API credentials (HTTP {r.status_code}) — "
                  "check REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET and that the app type is 'script'.",
                  flush=True)
    except Exception:
        pass
    return None


async def _get_reddit_json(client: httpx.AsyncClient, url: str) -> dict | None:
    """Fetch a Reddit thread as JSON, preferring the official OAuth Data API.

    Uses the canonical `/comments/<post_id>` endpoint on oauth.reddit.com (the
    approach proven in the standalone Reddit scraper) rather than rewriting the
    permalink and appending `.json` — permalink rewriting is unreliable for
    comment URLs. For a comment permalink we pass `comment=<id>` so Reddit
    returns that specific comment's context, which guarantees the target comment
    is in the payload instead of being collapsed behind a 'load more' stub.

    Falls back to anonymous `.json` when no API credentials are configured (that
    path is usually 403'd by Reddit now, but is kept so nothing regresses)."""
    token = await _reddit_oauth_token(client)
    post_id, comment_id = reddit_ids(url)

    for attempt in range(4):
        await _throttle()
        try:
            if token and post_id:
                params = {"raw_json": 1, "limit": 500, "sort": "top"}
                if comment_id:
                    # Focus the listing on this comment (plus a little context)
                    # so it can't be hidden behind a "more comments" stub.
                    params["comment"] = comment_id
                    params["context"] = 1
                r = await client.get(
                    f"https://oauth.reddit.com/comments/{post_id}",
                    params=params,
                    headers={"Authorization": f"Bearer {token}",
                             "User-Agent": config.REDDIT_USER_AGENT},
                    follow_redirects=True, timeout=30,
                )
                if r.status_code == 401:  # token expired mid-run -> refresh once
                    token = await _reddit_oauth_token(client, force=True)
                    continue
            else:
                json_url = url.rstrip("/") + "/.json"
                r = await client.get(
                    json_url, headers={"User-Agent": config.BROWSER_UA},
                    follow_redirects=True, timeout=20,
                )
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429:
                await asyncio.sleep(float(r.headers.get("retry-after", 5)))
                continue
            if r.status_code in (403, 500, 502, 503):
                await asyncio.sleep(2 * (attempt + 1))
                continue
        except Exception:
            await asyncio.sleep(1.5 * (attempt + 1))
    return None


# --- Reddit public RSS (no API key, no login, no cookie) --------------------
# Reddit 403s the anonymous `.json` endpoint and shows a CAPTCHA to automated
# browsers, but it still serves a PUBLIC Atom feed at `<post-url>/.rss` that
# needs no authentication at all. This is the approach proven in the standalone
# Reddit scraper (scrape_post_noauth.py) and is now the primary fetch path.
#
# Handy property for us: requesting the RSS of a COMMENT permalink returns just
# two entries — the parent post (t3_...) and that specific comment (t1_...) —
# which maps exactly onto the post_text / comment_text split the classifier
# needs. Verified live against Kaseya/N-able comment URLs.
_ATOM_NS = {"a": "http://www.w3.org/2005/Atom"}


def _reddit_html_to_text(raw: str | None) -> str:
    """Turn Reddit's markdown-HTML feed fragment into readable plain text."""
    if not raw:
        return ""
    text = _html.unescape(raw)
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)      # <!-- SC_OFF --> markers
    text = re.sub(r"</p\s*>|<br\s*/?>", "\n", text, flags=re.I)  # breaks -> newlines
    text = re.sub(r"<li>", "\n- ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)                          # drop remaining tags
    # Reddit appends a "submitted by /u/x [link] [comments]" footer to entries.
    text = re.sub(r"\s*submitted by\s*/u/\S+\s*(\[link\]\s*)?(\[comments\]\s*)?$",
                  "", text, flags=re.I)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


async def _get_reddit_rss(client: httpx.AsyncClient, url: str):
    """Fetch a Reddit post/comment as Atom entries. Returns a list of
    ElementTree entries, or None. No credentials required."""
    rss_url = url.split("?")[0].split("#")[0].rstrip("/") + "/.rss"
    for attempt in range(4):
        await _throttle()
        try:
            # Stay STATELESS, like the scraper's requests.get(). Reddit sets a
            # `session_tracker` cookie on the first response and then rate-limits
            # that session hard — replaying it made every request after the first
            # one in a batch return 429. Dropping the jar each time keeps every
            # fetch looking like a fresh visitor.
            try:
                client.cookies.clear()
            except Exception:
                pass
            r = await client.get(
                rss_url,
                params={"sort": "top", "limit": 100},
                headers={"User-Agent": config.BROWSER_UA,
                         "Accept": "application/atom+xml, */*"},
                follow_redirects=True, timeout=30,
            )
            _note_reddit_ratelimit(r)
            if "login" in str(r.url):      # bot wall -> no point retrying
                return None
            if r.status_code == 200:
                try:
                    return _ET.fromstring(r.content).findall("a:entry", _ATOM_NS)
                except Exception:
                    return None
            # Reddit alternates 429 and 403 ("Blocked") when the per-IP RSS
            # budget is spent; both mean "slow down", so back off hard rather
            # than burning retries that make it worse.
            if r.status_code in (429, 403):
                await asyncio.sleep(max(float(r.headers.get("retry-after", 8)), 8) * (attempt + 1))
                continue
            if r.status_code in (500, 502, 503):
                await asyncio.sleep(2 * (attempt + 1))
                continue
            return None
        except Exception:
            await asyncio.sleep(1.5 * (attempt + 1))
    return None


def reddit_parts_from_rss(entries, url: str, strict_comment: bool = False):
    """(post_text, comment_text, content_type) from Atom entries — the RSS twin
    of reddit_parts(). Entry ids are 't3_<post>' and 't1_<comment>'.

    strict_comment: set when the feed is a WHOLE THREAD (bulk mode). Without it
    we'd fall back to "the first comment in the feed", which in a thread fetch
    means attaching someone else's comment to this mention."""
    _, cid = reddit_ids(url)

    def _txt(el, tag):
        f = el.find(f"a:{tag}", _ATOM_NS)
        return f.text if f is not None else None

    post_text, comment_text, first_comment = "", "", ""
    for e in entries or []:
        eid = (_txt(e, "id") or "").strip()
        body = _reddit_html_to_text(_txt(e, "content"))
        if eid.startswith("t3_"):
            title = (_txt(e, "title") or "").strip()
            post_text = "\n\n".join(x for x in (title, body) if x)
        elif eid.startswith("t1_"):
            if not first_comment:
                first_comment = body
            if cid and eid.lower() == f"t1_{cid}".lower():
                comment_text = body
    if cid and not comment_text and not strict_comment:
        comment_text = first_comment   # focused feed: the one entry IS our comment
    return (post_text[: config.MAX_POST_CHARS],
            comment_text[: config.MAX_POST_CHARS],
            "comment" if cid else "post")


def reddit_post_url(url: str) -> str | None:
    """The parent POST url for any Reddit permalink — used to fetch a whole
    thread once instead of one request per comment."""
    pid, _ = reddit_ids(url)
    if not pid:
        return None
    m = re.match(r"(https?://[^/]*reddit\.com/r/[^/]+/comments/[a-z0-9]+)", url, re.I)
    if m:
        return m.group(1)
    return f"https://www.reddit.com/comments/{pid}"


def enrich_from_reddit_rss(p: dict, entries, fallback: str = "",
                           strict_comment: bool = False) -> bool:
    """Populate a post dict from Atom entries. Returns True if real text was
    found (so callers know whether to fall back to another fetch method)."""
    post_text, comment_text, ctype = reddit_parts_from_rss(
        entries, p.get("permalink", ""), strict_comment=strict_comment)
    if not (post_text or comment_text):
        return False
    p["content_type"] = ctype
    p["post_text"] = post_text or fallback
    p["comment_text"] = comment_text
    if ctype == "comment":
        p["text"] = (f"[PARENT POST]\n{post_text}\n\n[COMMENT]\n{comment_text}".strip()
                     or fallback)
    else:
        p["text"] = post_text or fallback
    # A comment permalink with no comment body is not usable for judging the
    # comment's own sentiment — treat it as a miss so a fallback can try.
    return bool(comment_text) if ctype == "comment" else bool(post_text)


async def _fetch_reddit_bulk_rss(client: httpx.AsyncClient, posts: list[dict]) -> list[dict]:
    """Credential-free bulk fetch: the same thread de-duplication as the OAuth
    path, but over Reddit's PUBLIC RSS.

    Why sequential: Reddit meters anonymous RSS at roughly ONE request per
    window (confirmed from its own x-ratelimit-remaining/reset headers), so
    concurrency buys nothing — _throttle() paces off those headers instead.
    Grouping is what actually buys speed here: on real data 53 mentions live in
    only 21 threads, and one thread feed carried 12 of 13 targets — ~60% fewer
    requests, so ~60% less wall-clock.

    A thread whose only target IS a comment is fetched via that comment's own
    permalink instead: the focused feed returns it directly, which avoids
    spending a second request to rescue it."""
    threads: dict[str, list[dict]] = {}
    for p in posts:
        pid, _ = reddit_ids(p.get("permalink", ""))
        if pid:
            threads.setdefault(pid, []).append(p)

    misses: list[dict] = []
    for pid, members in threads.items():
        _, only_cid = reddit_ids(members[0].get("permalink", "")) if len(members) == 1 else (None, None)
        if len(members) == 1 and only_cid:
            # single comment in this thread -> focused feed, one guaranteed hit
            p = members[0]
            entries = await _get_reddit_rss(client, p.get("permalink", ""))
            if entries:
                enrich_from_reddit_rss(p, entries, p.get("excerpt", "") or "")
            continue

        entries = await _get_reddit_rss(client, f"https://www.reddit.com/comments/{pid}")
        if not entries:
            continue
        for p in members:
            # strict: never borrow a different comment out of the thread feed
            enrich_from_reddit_rss(p, entries, p.get("excerpt", "") or "", strict_comment=True)
            if p.get("content_type") == "comment" and not p.get("comment_text"):
                misses.append(p)

    if misses:
        print(f"bulk[rss]: {len(misses)} comment(s) not inlined — re-fetching individually",
              flush=True)
        for p in misses:
            entries = await _get_reddit_rss(client, p.get("permalink", ""))
            if entries:
                enrich_from_reddit_rss(p, entries, p.get("excerpt", "") or "")
    return posts


# --- Apify Reddit scraper (paid) --------------------------------------------
# One record per submitted URL — a post URL returns the post, a comment
# permalink returns THAT comment (verified live: 36 mentions -> 36 records in
# 10s). Each record echoes the submitted URL back in `query`, so mapping results
# to mentions is an exact lookup rather than fuzzy matching.

_APIFY_DELETED = {"[deleted]", "[removed]", "[deleted by user]"}


def _url_key(u: str) -> str:
    """Loose URL key for matching Apify's echoed `query` back to our mention.
    Local on purpose — apply_tags.norm_permalink lives in the Playwright module
    and importing it here would drag that dependency into classification."""
    if not u:
        return ""
    return u.split("?")[0].split("#")[0].rstrip("/").lower()


def _apify_is_deleted(rec: dict) -> bool:
    if rec.get("is_deleted_or_removed") is True:
        return True
    body = (rec.get("body") or rec.get("selftext") or "").strip().lower()
    return body in {d.lower() for d in _APIFY_DELETED}


def _apify_apply_record(p: dict, rec: dict) -> None:
    """Fill a mention dict from one Apify record."""
    kind = (rec.get("kind") or "").lower()
    title = (rec.get("title") or "").strip()
    body = (rec.get("body") or rec.get("selftext") or "").strip()
    _, cid = reddit_ids(p.get("permalink", ""))
    is_comment = kind == "comment" or bool(cid)

    if _apify_is_deleted(rec):
        # Content no longer exists on Reddit. Flag it so the classifier can tag
        # Neutral with "no post or comment found" instead of guessing.
        p["content_type"] = "comment" if is_comment else "post"
        p["post_text"] = ""
        p["comment_text"] = ""
        p["text"] = ""
        p["deleted"] = True
        return

    if is_comment:
        p["content_type"] = "comment"
        p["comment_text"] = body[: config.MAX_POST_CHARS]
        # The actor gives the parent post's title/url but not its selftext;
        # the title alone is useful context for judging the comment.
        p["post_text"] = (rec.get("postTitle") or title or "")[: config.MAX_POST_CHARS]
        p["text"] = (f"[PARENT POST]\n{p['post_text']}\n\n[COMMENT]\n{p['comment_text']}").strip()
    else:
        p["content_type"] = "post"
        p["post_text"] = "\n\n".join(x for x in (title, body) if x)[: config.MAX_POST_CHARS]
        p["comment_text"] = ""
        p["text"] = p["post_text"]


async def fetch_via_apify(posts: list[dict]) -> list[dict]:
    """Fetch every Reddit mention through the Apify actor.

    Returns the posts unchanged (so the caller can fall back) when no
    APIFY_TOKEN is configured."""
    if not config.APIFY_TOKEN:
        print("apify: APIFY_TOKEN not set — cannot use the Apify path", flush=True)
        return posts

    targets = [p for p in posts if "reddit.com" in (p.get("permalink") or "")]
    if not targets:
        return posts

    # Token goes in the Authorization HEADER, never the query string — httpx logs
    # the full URL, so a ?token=... would end up in plain text in the app logs.
    url = f"https://api.apify.com/v2/acts/{config.APIFY_ACTOR}/run-sync-get-dataset-items"
    headers = {"Authorization": f"Bearer {config.APIFY_TOKEN}"}

    async with httpx.AsyncClient(timeout=config.APIFY_TIMEOUT + 30) as client:
        for start in range(0, len(targets), config.APIFY_BATCH_SIZE):
            chunk = targets[start:start + config.APIFY_BATCH_SIZE]
            payload = {
                "urls": [p["permalink"] for p in chunk],
                # We judge each mention on its OWN content, so never pull a
                # thread's other comments (they'd also be billed).
                "scrapeComments": False,
                "maxPosts": len(chunk) + 10,
                "maxComments": len(chunk) + 10,
            }
            try:
                r = await client.post(url, json=payload, headers=headers)
                if r.status_code not in (200, 201):
                    print(f"apify: run failed (HTTP {r.status_code}): {r.text[:200]}", flush=True)
                    continue
                records = r.json()
            except Exception as e:
                print(f"apify: run errored: {type(e).__name__}: {e}", flush=True)
                continue

            # Index results by the URL we submitted (`query`), then by ids.
            by_query, by_id = {}, {}
            for rec in records or []:
                q = (rec.get("query") or "").strip()
                if q:
                    by_query[_url_key(q)] = rec
                rid = (rec.get("id") or "").lower()
                if rid:
                    by_id[rid] = rec

            for p in chunk:
                link = p.get("permalink", "")
                pid, cid = reddit_ids(link)
                rec = (by_query.get(_url_key(link))
                       or (by_id.get(cid) if cid else None)
                       or (by_id.get(pid) if pid and not cid else None))
                if rec:
                    _apify_apply_record(p, rec)

            got = sum(1 for p in chunk if p.get("text") or p.get("deleted"))
            print(f"apify: {got}/{len(chunk)} mention(s) resolved "
                  f"({len(records or [])} record(s) returned)", flush=True)

            # The actor's direct comment-permalink lookup is unreliable: for some
            # comments it returns nothing at all (observed taking 30-60s and
            # yielding 0 records) even though the comment is live and IS returned
            # when its parent thread is scraped. Recover those by scraping the
            # parent thread once and matching the comment by id. Kept as a
            # fallback only — a thread scrape bills every comment it returns
            # (~68 records where we need 1), so it must never be the default.
            await _apify_recover_via_threads(
                client, url, headers,
                [p for p in chunk if not (p.get("text") or p.get("deleted"))])
    return posts


async def _apify_recover_via_threads(client, url, headers, missing: list[dict]) -> None:
    """Second pass for mentions the direct lookup missed: one thread scrape per
    distinct parent thread, then match each mention by its comment id."""
    if not missing:
        return
    by_thread: dict[str, list[dict]] = {}
    for p in missing:
        pid, _cid = reddit_ids(p.get("permalink", ""))
        if pid:
            by_thread.setdefault(pid, []).append(p)
    if not by_thread:
        return
    print(f"apify: retrying {len(missing)} unresolved mention(s) via "
          f"{len(by_thread)} parent thread(s)", flush=True)

    for pid, group in by_thread.items():
        sub = group[0].get("permalink", "").split("/comment/")[0]
        try:
            r = await client.post(url, headers=headers, json={
                "urls": [sub],
                "scrapeComments": True,       # needed to reach the comment
                "maxPosts": 2,
                "maxComments": config.APIFY_THREAD_MAX_COMMENTS,
            })
            if r.status_code not in (200, 201):
                print(f"apify: thread retry failed for {pid} (HTTP {r.status_code})", flush=True)
                continue
            records = r.json() or []
        except Exception as e:
            print(f"apify: thread retry errored for {pid}: {type(e).__name__}: {e}", flush=True)
            continue

        by_id = {(rec.get("id") or "").lower(): rec for rec in records if rec.get("id")}
        # Thread-scraped comment records carry no postTitle, so take the parent
        # context from the post record that came back in the same scrape.
        parent = next((rec for rec in records if rec.get("kind") == "post"), None)
        parent_ctx = ""
        if parent:
            parent_ctx = "\n\n".join(x for x in (parent.get("title"),
                                                 parent.get("body") or parent.get("selftext")) if x)
        for p in group:
            _pid, cid = reddit_ids(p.get("permalink", ""))
            rec = by_id.get(cid) if cid else by_id.get(_pid)
            if rec:
                _apify_apply_record(p, rec)
                if parent_ctx and p.get("content_type") == "comment" and not p.get("post_text"):
                    p["post_text"] = parent_ctx[: config.MAX_POST_CHARS]
                    p["text"] = (f"[PARENT POST]\n{p['post_text']}\n\n"
                                 f"[COMMENT]\n{p.get('comment_text','')}").strip()
                print(f"apify: recovered {p.get('permalink')} from parent thread", flush=True)
            else:
                print(f"apify: still unresolved after thread retry: {p.get('permalink')}",
                      flush=True)


async def fetch_reddit_scraper_bulk(posts: list[dict]) -> list[dict]:
    """Public entry point for the credential-free grouped RSS fetch — what the
    'Reddit Scrapper' mode uses. Same thread de-duplication as the OAuth bulk
    path, so a batch costs one request per THREAD instead of one per mention."""
    async with httpx.AsyncClient(timeout=30) as client:
        return await _fetch_reddit_bulk_rss(client, posts)



async def fetch_full_text(client: httpx.AsyncClient, url: str, fallback: str) -> str:
    """Best-effort fetch of full post text. Reddit -> JSON; else fall back to excerpt."""
    fallback = fallback or ""
    if not url or not isinstance(url, str):
        return fallback
    try:
        if "reddit.com" in url:
            # Public RSS first (no credentials), then the JSON API.
            entries = await _get_reddit_rss(client, url)
            if entries:
                post_text, comment_text, ctype = reddit_parts_from_rss(entries, url)
                combined = (f"[PARENT POST]\n{post_text}\n\n[COMMENT]\n{comment_text}".strip()
                            if ctype == "comment" and comment_text else post_text)
                if combined:
                    return combined
            payload = await _get_reddit_json(client, url)
            if payload:
                return _reddit_text(payload) or fallback
        else:
            await _throttle()
            r = await client.get(
                url, headers={"User-Agent": config.BROWSER_UA},
                follow_redirects=True, timeout=20,
            )
            if r.status_code == 200:
                text = re.sub(r"<[^>]+>", " ", r.text)
                text = re.sub(r"\s+", " ", text)
                return text.strip()[: config.MAX_POST_CHARS] or fallback
    except Exception:
        pass
    return fallback


async def fetch_and_enrich(client: httpx.AsyncClient, p: dict, prefer: str = "rss") -> dict:
    """Fetch a post dict's text and populate content_type / post_text /
    comment_text / text (post vs comment aware).

    prefer="rss"  -> public Atom feed first (no key/login), JSON API as fallback.
    prefer="api"  -> official OAuth JSON API first, RSS as fallback.
    Whichever runs first, the other is still tried, so a single blocked route
    never loses the post."""
    url = p.get("permalink", "")
    fallback = p.get("excerpt", "") or ""

    async def _try_rss() -> bool:
        entries = await _get_reddit_rss(client, url)
        return bool(entries) and enrich_from_reddit_rss(p, entries, fallback)

    async def _try_json() -> bool:
        payload = await _get_reddit_json(client, url)
        if payload:
            enrich_from_reddit_payload(p, payload, fallback)
            return True
        return False

    try:
        if url and "reddit.com" in url:
            order = (_try_json, _try_rss) if prefer == "api" else (_try_rss, _try_json)
            for attempt in order:
                if await attempt():
                    return p
        elif url:
            await _throttle()
            r = await client.get(
                url, headers={"User-Agent": config.BROWSER_UA},
                follow_redirects=True, timeout=20,
            )
            if r.status_code == 200:
                text = re.sub(r"<[^>]+>", " ", r.text)
                text = re.sub(r"\s+", " ", text).strip()[: config.MAX_POST_CHARS]
                set_plain_text(p, text or fallback)
                return p
    except Exception:
        pass
    set_plain_text(p, fallback)
    return p


def _reddit_text(payload) -> str:
    parts = []
    try:
        listing = payload[0]["data"]["children"]
        for child in listing:
            d = child["data"]
            if d.get("title"):
                parts.append(d["title"])
            if d.get("selftext"):
                parts.append(d["selftext"])
        # top comments add context for comment-based mentions
        if len(payload) > 1:
            for child in payload[1]["data"]["children"][:10]:
                body = child.get("data", {}).get("body")
                if body:
                    parts.append(body)
    except Exception:
        pass
    return "\n\n".join(parts)[: config.MAX_POST_CHARS]


def reddit_ids(url: str):
    """(post_id, comment_id or None) from a Reddit URL. Handles both the
    /comments/<post>/comment/<cid>/ and older /comments/<post>/<slug>/<cid>/
    forms (mirrors norm_permalink so classification and apply agree)."""
    if not url or not isinstance(url, str) or "reddit.com" not in url:
        return None, None
    clean = url.split("?")[0].split("#")[0]
    m = re.search(r"/comments/([a-z0-9]+)", clean, re.I)
    if not m:
        return None, None
    post_id = m.group(1).lower()
    cm = re.search(r"/comment/([a-z0-9]+)", clean, re.I)
    if cm:
        return post_id, cm.group(1).lower()
    after = clean[m.end():]
    segs = [s for s in after.split("/") if s]
    if len(segs) >= 2 and re.fullmatch(r"[a-z0-9]{4,}", segs[-1], re.I):
        return post_id, segs[-1].lower()
    return post_id, None


def reddit_content_type(url: str) -> str:
    """'comment' if the URL points at a specific comment, else 'post'
    (non-Reddit sources are treated as standalone posts)."""
    _, cid = reddit_ids(url)
    return "comment" if cid else "post"


def _reddit_post_selftext(payload) -> str:
    """Just the parent post's title + selftext (no thread comments)."""
    parts = []
    try:
        d = payload[0]["data"]["children"][0]["data"]
        if d.get("title"):
            parts.append(d["title"])
        if d.get("selftext"):
            parts.append(d["selftext"])
    except Exception:
        pass
    return "\n\n".join(parts)[: config.MAX_POST_CHARS]


def _find_comment_body(node: dict, cid: str) -> str:
    """Depth-first search for a specific comment id's body in a Reddit comment
    listing tree (walks nested replies)."""
    data = node.get("data", {}) if isinstance(node, dict) else {}
    if str(data.get("id", "")).lower() == cid and data.get("body"):
        return data["body"]
    replies = data.get("replies")
    if isinstance(replies, dict):
        for ch in replies.get("data", {}).get("children", []):
            found = _find_comment_body(ch, cid)
            if found:
                return found
    return ""


def reddit_parts(payload, url: str, strict_comment: bool = False):
    """Return (post_text, comment_text, content_type) from a Reddit .json payload.

    For a comment URL, comment_text is the SPECIFIC comment's body and post_text
    is the parent post — kept separate so the classifier judges the comment on
    its own content with the post only as context. For a post URL, comment_text
    is empty and post_text is the post itself.

    strict_comment: when the payload is a WHOLE THREAD (bulk mode) rather than a
    focused single-comment fetch, never fall back to "the first comment in the
    listing" — that would silently attach a different user's comment to this
    mention. Returning empty instead lets the caller re-fetch or flag it."""
    post_id, cid = reddit_ids(url)
    post_text = _reddit_post_selftext(payload)
    comment_text = ""
    if cid and isinstance(payload, list) and len(payload) > 1:
        try:
            for ch in payload[1]["data"]["children"]:
                comment_text = _find_comment_body(ch, cid)
                if comment_text:
                    break
            if not comment_text and not strict_comment:
                # focused fetch: the listing IS this comment's context
                first = payload[1]["data"]["children"][0]["data"]
                comment_text = first.get("body", "") or ""
        except Exception:
            pass
        comment_text = comment_text[: config.MAX_POST_CHARS]
    return post_text, comment_text, ("comment" if cid else "post")


def enrich_from_reddit_payload(p: dict, payload, fallback: str = "",
                               strict_comment: bool = False) -> None:
    """Populate a post dict's content_type / post_text / comment_text / text
    from a Reddit .json payload. `text` stays a sensible combined string for
    backward compatibility (and as the classifier's fallback)."""
    post_text, comment_text, ctype = reddit_parts(payload, p.get("permalink", ""),
                                                  strict_comment=strict_comment)
    p["content_type"] = ctype
    p["post_text"] = post_text or fallback
    p["comment_text"] = comment_text
    if ctype == "comment":
        p["text"] = (f"[PARENT POST]\n{post_text}\n\n[COMMENT]\n{comment_text}".strip()
                     or fallback)
    else:
        p["text"] = post_text or fallback


def set_plain_text(p: dict, text: str) -> None:
    """For non-Reddit sources, or when the Reddit JSON fetch failed and only the
    Meltwater excerpt is available. A Meltwater excerpt for a comment mention is
    the comment's own text, so route it to comment_text in that case."""
    text = text or ""
    ctype = reddit_content_type(p.get("permalink", ""))
    p["content_type"] = ctype
    p["text"] = text
    if ctype == "comment":
        p["post_text"] = ""          # no separate parent text available in fallback
        p["comment_text"] = text
    else:
        p["post_text"] = text
        p["comment_text"] = ""


# --- Classification --------------------------------------------------------

async def classify_post(
    anthropic: AsyncAnthropic, run_brand: str, permalink: str, text: str, sem: asyncio.Semaphore
) -> dict:
    async with sem:
        try:
            resp = await anthropic.messages.create(
                model=config.MODEL,
                max_tokens=16000,
                thinking={"type": "adaptive"},
                system=SYSTEM_PROMPT.format(run_brand=run_brand),
                messages=[{
                    "role": "user",
                    "content": POST_TEMPLATE.format(
                        run_brand=run_brand, permalink=permalink, text=text or "(no text available)"
                    ),
                }],
                output_config={"format": {"type": "json_schema", "schema": DECISION_SCHEMA}},
            )
            raw = next(b.text for b in resp.content if b.type == "text")
            decision = json.loads(raw)
        except Exception as e:
            return {
                "permalink": permalink,
                "action": "review",
                "reason": f"classification error: {e}",
                "tag": None,
            }

    return _resolve(run_brand, permalink, decision)


def _resolve(run_brand: str, permalink: str, d: dict) -> dict:
    """Turn a model decision into a concrete tag-to-apply or a flag reason."""
    action = d.get("action")
    out = {"permalink": permalink, "action": action, "reason": d.get("reason", ""), "tag": None}

    if action == "apply":
        brand = normalize_brand(d.get("primary_brand", "")) or run_brand
        sentiment = d.get("sentiment", "")
        if brand == run_brand and is_valid_tag(sentiment, brand):
            out["tag"] = tag_name(sentiment, brand)
        else:
            # model said apply but brand/sentiment doesn't line up -> flag for safety
            out["action"] = "review"
            out["reason"] = (
                f"model said apply but resolved brand={brand} sentiment={sentiment}; "
                + out["reason"]
            )
    elif action == "skip_flag":
        other = normalize_brand(d.get("primary_brand", "")) or d.get("primary_brand", "unknown")
        out["flag_brand"] = other
    # review / paywall pass through with reason
    return out


# --- Browser-based fetching (logged-in Reddit, no API key) -----------------

async def reddit_login():
    """Open the script's persistent browser so you can log into Reddit once."""
    from playwright.async_api import async_playwright
    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(config.USER_DATA_DIR, headless=False)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto("https://www.reddit.com/login/")
        input("\n>>> Log into Reddit in the browser window, then press Enter here to save the session...\n")
        await ctx.close()
    print("Reddit session saved. Re-run classify with --browser.")


async def fetch_via_browser(posts):
    """Fetch full text using the logged-in persistent browser profile."""
    from playwright.async_api import async_playwright
    sem = asyncio.Semaphore(max(1, config.FETCH_CONCURRENCY))
    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            config.USER_DATA_DIR, headless=config.HEADLESS,
            user_agent=config.BROWSER_UA,
        )

        async def one(p):
            url = p["permalink"]
            async with sem:
                page = await ctx.new_page()
                try:
                    if "reddit.com" in url:
                        await page.goto(url.rstrip("/") + "/.json", wait_until="domcontentloaded", timeout=30000)
                        body = await page.evaluate("() => document.body.innerText")
                        try:
                            enrich_from_reddit_payload(p, json.loads(body), p.get("excerpt", ""))
                        except Exception:
                            set_plain_text(p, p.get("excerpt", ""))
                    else:
                        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                        txt = await page.evaluate("() => document.body.innerText")
                        set_plain_text(p, (txt or "").strip()[: config.MAX_POST_CHARS] or p.get("excerpt", ""))
                except Exception:
                    set_plain_text(p, p.get("excerpt", ""))
                finally:
                    await page.close()
            return p

        posts = await asyncio.gather(*[one(p) for p in posts])
        await ctx.close()
    return posts


async def fetch_via_cdp(posts):
    """Fetch full text by attaching to a real Chrome started with a debug port."""
    from playwright.async_api import async_playwright
    sem = asyncio.Semaphore(max(1, config.FETCH_CONCURRENCY))
    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.connect_over_cdp(config.CHROME_CDP_URL)
        except Exception as e:
            sys.exit(
                f"\nCould not connect to Chrome at {config.CHROME_CDP_URL}: {e}\n"
                "Start Chrome with a debug port first (see README 'Option B'):\n"
                '  & "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" '
                '--remote-debugging-port=9222 --user-data-dir="C:\\mw-chrome-profile"\n'
                "Then log into Reddit in that window and re-run with --cdp.\n"
            )
        # Use the existing (logged-in) context so Reddit cookies are present.
        ctx = browser.contexts[0] if browser.contexts else await browser.new_context()

        async def one(p):
            url = p["permalink"]
            async with sem:
                page = await ctx.new_page()
                try:
                    if "reddit.com" in url:
                        await page.goto(url.rstrip("/") + "/.json", wait_until="domcontentloaded", timeout=30000)
                        body = await page.evaluate("() => document.body.innerText")
                        try:
                            enrich_from_reddit_payload(p, json.loads(body), p.get("excerpt", ""))
                        except Exception:
                            set_plain_text(p, p.get("excerpt", ""))
                    else:
                        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                        txt = await page.evaluate("() => document.body.innerText")
                        set_plain_text(p, (txt or "").strip()[: config.MAX_POST_CHARS] or p.get("excerpt", ""))
                except Exception:
                    set_plain_text(p, p.get("excerpt", ""))
                finally:
                    await page.close()
            return p

        posts = await asyncio.gather(*[one(p) for p in posts])
        # don't close the user's Chrome; just detach
    return posts


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("export", nargs="?", help="Path to the Meltwater Excel export (.xlsx)")
    ap.add_argument("--brand", help="Run brand (e.g. Kaseya). Inferred from topic if omitted.")
    ap.add_argument("--out", default=config.DECISIONS_FILE)
    ap.add_argument("--browser", action="store_true",
                    help="Fetch full text via the logged-in persistent browser (no Reddit API key).")
    ap.add_argument("--cdp", action="store_true",
                    help="Fetch full text by attaching to your real Chrome (started with "
                         "--remote-debugging-port). Best for Reddit's bot wall. See README Option B.")
    ap.add_argument("--reddit-login", action="store_true",
                    help="Open the browser to log into Reddit once, then exit.")
    args = ap.parse_args()

    if args.reddit_login:
        await reddit_login()
        return
    if not args.export:
        ap.error("export file is required (unless using --reddit-login)")

    df, cols = load_export(args.export)
    run_brand = args.brand or infer_brand(df, cols["topic"])
    if not run_brand:
        sys.exit("Could not infer run brand; pass --brand (e.g. --brand Kaseya).")
    print(f"Run brand: {run_brand}  |  posts in export: {len(df)}  |  model: {config.MODEL}")

    # Build the work list, skipping rows already tagged (per the export, if present).
    posts = []
    already_tagged = []
    for _, row in df.iterrows():
        permalink = str(row[cols["permalink"]]).strip()
        if not permalink or permalink.lower() == "nan":
            continue
        existing = str(row[cols["tags"]]).strip() if cols["tags"] else ""
        # Detect an existing sentiment tag in either order:
        # "Kaseya - positive" (account format) or "Positive - Kaseya".
        if existing and existing.lower() != "nan" and re.search(
            r"-\s*(positive|negative|neutral)|(positive|negative|neutral)\s*-", existing, re.I
        ):
            already_tagged.append({"permalink": permalink, "existing_tags": existing})
            continue
        excerpt = ""
        if cols["text"]:
            v = str(row[cols["text"]]).strip()
            if v and v.lower() != "nan":
                excerpt = v
        posts.append({"permalink": permalink, "excerpt": excerpt})

    print(f"To classify: {len(posts)}  |  already-tagged (skipped): {len(already_tagged)}")

    # 1) Fetch full text in parallel.
    if args.cdp:
        posts = await fetch_via_cdp(posts)
        mode = "attached Chrome (CDP)"
    elif args.browser:
        posts = await fetch_via_browser(posts)
        mode = "logged-in browser"
    else:
        fetch_sem = asyncio.Semaphore(config.FETCH_CONCURRENCY)
        async with httpx.AsyncClient() as http:
            async def _fetch(p):
                async with fetch_sem:
                    p["text"] = await fetch_full_text(http, p["permalink"], p["excerpt"])
                return p
            posts = await asyncio.gather(*[_fetch(p) for p in posts])
        mode = ("Reddit API (OAuth)" if (config.REDDIT_CLIENT_ID and config.REDDIT_CLIENT_SECRET)
                else "anonymous (throttled)")
    got = sum(1 for p in posts if p.get("text"))
    print(f"Full text fetched for {got}/{len(posts)} posts  [{mode}]")
    if got < len(posts) * 0.5:
        print("  WARNING: most posts have no text — classifications will be unreliable.")
        if not args.browser:
            print("  Reddit is rate-limiting anonymous fetch. Either:")
            print("    - run once:  python classify.py --reddit-login   (log into Reddit)")
            print("      then add --browser to your classify command, OR")
            print("    - add Reddit API creds to .env (REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET).")
        elif args.browser:
            print("  Logged-in Playwright browser is likely still flagged by Reddit's bot wall. "
                  "Use --cdp (attach to your real Chrome) instead — see README Option B.")
        else:
            print("  Attached Chrome returned no text — make sure you are logged into Reddit "
                  "in that Chrome window and the URLs open normally there.")

    # 2) Classify in parallel.
    anthropic = AsyncAnthropic()
    # Preflight: catch a bad/missing API key now instead of failing all N posts.
    try:
        await anthropic.messages.create(
            model=config.MODEL, max_tokens=8,
            messages=[{"role": "user", "content": "ping"}],
        )
    except AuthenticationError:
        sys.exit(
            "\nERROR: invalid or missing ANTHROPIC_API_KEY.\n"
            "Set it in this terminal and re-run, e.g. (PowerShell):\n"
            '  $env:ANTHROPIC_API_KEY="sk-ant-..."\n'
        )
    except APIStatusError as e:
        msg = str(getattr(e, "message", e))
        if "credit" in msg.lower() or "billing" in msg.lower():
            sys.exit(
                "\nERROR: Anthropic API has no credit balance.\n"
                "Add credits at https://console.anthropic.com -> Plans & Billing,\n"
                "then re-run. (Full text was already fetched; this is purely billing.)\n"
            )
        sys.exit(f"\nERROR from Anthropic API: {msg}\n")
    class_sem = asyncio.Semaphore(config.CLASSIFY_CONCURRENCY)
    decisions = await asyncio.gather(
        *[classify_post(anthropic, run_brand, p["permalink"], p["text"], class_sem) for p in posts]
    )

    result = {
        "run_brand": run_brand,
        "already_tagged": already_tagged,
        "decisions": decisions,
        "export_count": len(df),
    }
    # decisions.json is the internal handoff that apply_tags.py reads.
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    # Human-facing results: timestamped Excel under results/.
    rows = [{
        "action": d.get("action"),
        "tag_to_apply": d.get("tag") or "",
        "flag_brand": d.get("flag_brand", ""),
        "reason": d.get("reason", ""),
        "permalink": d.get("permalink"),
    } for d in decisions]
    xlsx = write_results_excel(rows, kind="classification", brand=run_brand)

    n_apply = sum(1 for d in decisions if d["tag"])
    print(f"\nDone. {n_apply} tags to apply.")
    print(f"Excel results -> {xlsx}")
    print("Next: review the Excel, then run  python apply_tags.py")


if __name__ == "__main__":
    # On Windows the default Proactor loop spams "Event loop is closed" on shutdown
    # when httpx/anthropic sockets are GC'd — the selector loop avoids that noise.
    # BUT Playwright needs the Proactor loop to spawn the browser subprocess, so only
    # switch to the selector loop when we're NOT using the browser fetch modes.
    if sys.platform.startswith("win") and not (
        "--browser" in sys.argv or "--reddit-login" in sys.argv or "--cdp" in sys.argv
    ):
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
