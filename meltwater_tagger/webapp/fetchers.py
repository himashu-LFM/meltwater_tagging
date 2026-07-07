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

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import httpx

import config
from classify import _reddit_text


async def fetch_via_reddit_cookie(posts: list[dict], cookie_value: str) -> list[dict]:
    if not cookie_value:
        for p in posts:
            p["text"] = p.get("excerpt", "")
        return posts

    cookies = {"reddit_session": cookie_value}
    sem = asyncio.Semaphore(max(1, config.FETCH_CONCURRENCY))

    async with httpx.AsyncClient(cookies=cookies, headers={"User-Agent": config.BROWSER_UA}) as client:
        async def one(p):
            url = p["permalink"]
            async with sem:
                try:
                    if "reddit.com" in url:
                        r = await client.get(url.rstrip("/") + "/.json", follow_redirects=True, timeout=20)
                        if r.status_code == 200:
                            p["text"] = _reddit_text(r.json()) or p.get("excerpt", "")
                        else:
                            p["text"] = p.get("excerpt", "")
                    else:
                        r = await client.get(url, follow_redirects=True, timeout=20)
                        text = re.sub(r"<[^>]+>", " ", r.text) if r.status_code == 200 else ""
                        p["text"] = re.sub(r"\s+", " ", text).strip()[: config.MAX_POST_CHARS] or p.get("excerpt", "")
                except Exception:
                    p["text"] = p.get("excerpt", "")
            return p

        posts = await asyncio.gather(*[one(p) for p in posts])
    return posts
