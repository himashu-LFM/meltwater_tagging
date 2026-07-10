"""
Reddit fetch using a pasted session cookie — works on a cloud host with no
local Chrome and no Reddit API key. The analyst logs into reddit.com in their
own browser once, copies the 'reddit_session' cookie value (dev tools ->
Application -> Cookies -> reddit.com -> reddit_session), and pastes it into
their profile. We attach it to plain HTTP requests, which Reddit treats as a
normal logged-in session rather than a bot.

Falls back to the post's Meltwater excerpt if the cookie is missing/expired.
"""

import asyncio
import re
import sys
import os

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_THIS_DIR))
sys.path.insert(0, _THIS_DIR)
import httpx

import config
from classify import _reddit_text
from logging_setup import get_logger

log = get_logger("fetchers")


async def fetch_via_reddit_cookie(posts: list[dict], cookie_value: str) -> list[dict]:
    if not cookie_value:
        log.warning("fetch_via_reddit_cookie: no cookie provided — using Meltwater excerpts only for %d posts", len(posts))
        for p in posts:
            p["text"] = p.get("excerpt", "")
        return posts

    cookies = {"reddit_session": cookie_value}
    sem = asyncio.Semaphore(max(1, config.FETCH_CONCURRENCY))
    failures = 0

    async with httpx.AsyncClient(cookies=cookies, headers={"User-Agent": config.BROWSER_UA}) as client:
        async def one(p):
            nonlocal failures
            url = p["permalink"]
            async with sem:
                try:
                    if "reddit.com" in url:
                        r = await client.get(url.rstrip("/") + "/.json", follow_redirects=True, timeout=20)
                        if r.status_code == 200:
                            p["text"] = _reddit_text(r.json()) or p.get("excerpt", "")
                        else:
                            log.warning("reddit cookie fetch got status=%s for %s", r.status_code, url)
                            failures += 1
                            p["text"] = p.get("excerpt", "")
                    else:
                        r = await client.get(url, follow_redirects=True, timeout=20)
                        text = re.sub(r"<[^>]+>", " ", r.text) if r.status_code == 200 else ""
                        p["text"] = re.sub(r"\s+", " ", text).strip()[: config.MAX_POST_CHARS] or p.get("excerpt", "")
                except Exception as e:
                    log.warning("reddit cookie fetch error for %s: %s: %s", url, type(e).__name__, e)
                    failures += 1
                    p["text"] = p.get("excerpt", "")
            return p

        posts = await asyncio.gather(*[one(p) for p in posts])

    log.info("fetch_via_reddit_cookie: %d/%d posts fetched OK (%d failed)",
              len(posts) - failures, len(posts), failures)
    return posts
