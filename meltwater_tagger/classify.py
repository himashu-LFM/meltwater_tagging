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
import json
import re
import sys

import httpx
import pandas as pd
from anthropic import AsyncAnthropic, AuthenticationError, APIStatusError

# On Windows the default Proactor loop spams "Event loop is closed" on shutdown
# when httpx/anthropic sockets are GC'd — the selector loop avoids that noise.
# BUT Playwright needs the Proactor loop to spawn the browser subprocess, so only
# switch to the selector loop when we're NOT using the browser fetch modes.
if sys.platform.startswith("win") and not (
    "--browser" in sys.argv or "--reddit-login" in sys.argv or "--cdp" in sys.argv
):
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

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
_reddit_token: dict = {"value": None}


async def _throttle():
    global _reddit_last
    async with _reddit_lock:
        now = asyncio.get_event_loop().time()
        wait = config.REDDIT_MIN_INTERVAL - (now - _reddit_last)
        if wait > 0:
            await asyncio.sleep(wait)
        _reddit_last = asyncio.get_event_loop().time()


async def _reddit_oauth_token(client: httpx.AsyncClient) -> str | None:
    """Fetch an app-only OAuth token if credentials are configured."""
    if _reddit_token["value"]:
        return _reddit_token["value"]
    if not (config.REDDIT_CLIENT_ID and config.REDDIT_CLIENT_SECRET):
        return None
    try:
        r = await client.post(
            "https://www.reddit.com/api/v1/access_token",
            data={"grant_type": "client_credentials"},
            auth=(config.REDDIT_CLIENT_ID, config.REDDIT_CLIENT_SECRET),
            headers={"User-Agent": config.REDDIT_USER_AGENT},
            timeout=20,
        )
        if r.status_code == 200:
            _reddit_token["value"] = r.json().get("access_token")
            return _reddit_token["value"]
    except Exception:
        pass
    return None


async def _get_reddit_json(client: httpx.AsyncClient, url: str) -> dict | None:
    """Fetch a Reddit post as JSON via OAuth API if available, else anonymous .json."""
    token = await _reddit_oauth_token(client)
    for attempt in range(4):
        await _throttle()
        try:
            if token:
                api_url = re.sub(r"https?://(www\.)?reddit\.com", "https://oauth.reddit.com", url)
                api_url = api_url.rstrip("/") + "/.json"
                r = await client.get(
                    api_url,
                    headers={"Authorization": f"Bearer {token}", "User-Agent": config.REDDIT_USER_AGENT},
                    follow_redirects=True, timeout=20,
                )
            else:
                json_url = url.rstrip("/") + "/.json"
                r = await client.get(
                    json_url, headers={"User-Agent": config.BROWSER_UA},
                    follow_redirects=True, timeout=20,
                )
            if r.status_code == 200:
                return r.json()
            if r.status_code in (429, 403, 500, 503):
                await asyncio.sleep(2 * (attempt + 1))
                continue
        except Exception:
            await asyncio.sleep(1.5 * (attempt + 1))
    return None


async def fetch_full_text(client: httpx.AsyncClient, url: str, fallback: str) -> str:
    """Best-effort fetch of full post text. Reddit -> JSON; else fall back to excerpt."""
    fallback = fallback or ""
    if not url or not isinstance(url, str):
        return fallback
    try:
        if "reddit.com" in url:
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


# --- Classification --------------------------------------------------------

async def classify_post(
    anthropic: AsyncAnthropic, run_brand: str, permalink: str, text: str, sem: asyncio.Semaphore
) -> dict:
    async with sem:
        try:
            resp = await anthropic.messages.create(
                model=config.MODEL,
                max_tokens=2000,
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
                            p["text"] = _reddit_text(json.loads(body)) or p.get("excerpt", "")
                        except Exception:
                            p["text"] = p.get("excerpt", "")
                    else:
                        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                        txt = await page.evaluate("() => document.body.innerText")
                        p["text"] = (txt or "").strip()[: config.MAX_POST_CHARS] or p.get("excerpt", "")
                except Exception:
                    p["text"] = p.get("excerpt", "")
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
                            p["text"] = _reddit_text(json.loads(body)) or p.get("excerpt", "")
                        except Exception:
                            p["text"] = p.get("excerpt", "")
                    else:
                        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                        txt = await page.evaluate("() => document.body.innerText")
                        p["text"] = (txt or "").strip()[: config.MAX_POST_CHARS] or p.get("excerpt", "")
                except Exception:
                    p["text"] = p.get("excerpt", "")
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
    asyncio.run(main())
