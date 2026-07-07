"""
Automated "Apply to Meltwater" for the web UI — headless login + apply, no
manual browser step. Reuses the exact card-handling logic from apply_tags.py
(hover -> tag icon -> modal -> check -> Apply) so both entry points stay in sync.

IMPORTANT — the login step is a best-effort guess. We do not have Meltwater's
actual login-page field names (no public docs, same situation as the Reddit
selectors). If login fails, the report will say so explicitly; the selectors
in `MELTWATER_LOGIN_SELECTORS` below are the one place to adjust against the
real login page (open it once with dev tools and check the email/password
input attributes).
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from playwright.async_api import async_playwright

from apply_tags import (
    SELECTORS, norm_permalink, get_card_permalink, card_existing_tag,
    expand_similar_if_needed, apply_tag_to_card,
)
import config

MELTWATER_LOGIN_URL = os.environ.get("MELTWATER_LOGIN_URL", "https://app.meltwater.com/login")

# Best-effort guess at Meltwater's login form. Adjust if login fails —
# see the module docstring.
LOGIN_SELECTORS = {
    "email": 'input[type="email"], input[name="email"], input[name="username"]',
    "password": 'input[type="password"], input[name="password"]',
    "submit": 'button[type="submit"], button:has-text("Log in"), button:has-text("Sign in")',
}


async def login_to_meltwater(page, email: str, password: str) -> tuple[bool, str]:
    try:
        await page.goto(MELTWATER_LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
        email_field = await page.wait_for_selector(LOGIN_SELECTORS["email"], timeout=15000)
        await email_field.fill(email)

        # some SSO-style login flows require submitting the email first
        submit = await page.query_selector(LOGIN_SELECTORS["submit"])
        pwd_field = await page.query_selector(LOGIN_SELECTORS["password"])
        if not pwd_field and submit:
            await submit.click()
            pwd_field = await page.wait_for_selector(LOGIN_SELECTORS["password"], timeout=15000)

        await pwd_field.fill(password)
        submit = await page.query_selector(LOGIN_SELECTORS["submit"])
        if submit:
            await submit.click()

        await page.wait_for_load_state("networkidle", timeout=30000)
        if "login" in page.url.lower():
            return False, "Still on the login page after submit — check credentials or selectors."
        return True, "ok"
    except Exception as e:
        return False, f"Login automation failed: {e}. Meltwater's login form may differ from our guessed selectors."


async def apply_results_to_meltwater(email: str, password: str, topic_url: str, results: list[dict]) -> dict:
    """
    results: the classification results from the UI, each with permalink/tag/action.
    Only entries with action == 'apply' and a non-empty tag are actually applied.
    Returns a report dict: {ok, message, applied, skipped_already, untouched, failed}
    """
    to_apply = {norm_permalink(r["permalink"]): r["tag"] for r in results if r.get("action") == "apply" and r.get("tag")}
    if not to_apply:
        return {"ok": False, "message": "No posts with an 'apply' action to tag.", "applied": [], "failed": []}
    if not topic_url:
        return {"ok": False, "message": "This brand has no Meltwater topic URL configured yet (set it once in Brand settings).",
                "applied": [], "failed": []}

    applied, skipped_already, failed = [], [], []
    seen = set()
    delay = config.ACTION_DELAY_MS / 1000.0

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()

        ok, msg = await login_to_meltwater(page, email, password)
        if not ok:
            await browser.close()
            return {"ok": False, "message": msg, "applied": [], "failed": []}

        await page.goto(topic_url, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(2)

        stable_rounds = 0
        last_seen_count = -1
        while stable_rounds < 3:
            cards = await page.query_selector_all('[data-testid="result-card"], article')
            for card in cards:
                permalink = await get_card_permalink(card)
                if not permalink or permalink in seen:
                    continue
                seen.add(permalink)

                await expand_similar_if_needed(card)

                existing = await card_existing_tag(card)
                if existing:
                    skipped_already.append({"permalink": permalink, "existing_tags": existing})
                    continue

                tag = to_apply.get(permalink)
                if not tag:
                    continue

                ok = await apply_tag_to_card(page, card, tag, dry_run=False, delay=delay)
                (applied if ok else failed).append({"permalink": permalink, "tag": tag})

            await page.mouse.wheel(0, 4000)
            await asyncio.sleep(0.8)
            if len(seen) == last_seen_count:
                stable_rounds += 1
            else:
                stable_rounds = 0
            last_seen_count = len(seen)

        await browser.close()

    unreached = [link for link in to_apply if link not in seen]
    return {
        "ok": True,
        "message": f"Applied {len(applied)} tag(s).",
        "applied": applied,
        "skipped_already": skipped_already,
        "failed": failed,
        "unreached": unreached,
    }
