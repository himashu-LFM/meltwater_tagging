"""
Bentley Phase-2 apply for the web dashboard — tag by Document ID via Meltwater's
internal tagging API.

Flow: log in with the analyst's saved Meltwater credentials (the same proven
login used by the sentiment apply), grab the app's OWN authorization header off
its BFF traffic, then POST each document's tag ids. No topic URL, no feed, no
scrolling — the document-ID API doesn't care what's on screen.
"""

import asyncio
import base64
import json
import re

from playwright.async_api import async_playwright

import config
from logging_setup import get_logger
from meltwater_apply import (login_to_meltwater, _new_browser_context, _is_logged_in,
                             switch_meltwater_account, CHROMIUM_LAUNCH_ARGS)
from brands.bentley import api_apply as aa

log = get_logger("bentley_apply_web")


def _tag_token(auth: str):
    """Return the RAW Meltwater tagging JWT from an Authorization header value,
    or None. The tagging BFF wants a JWT whose payload carries `company`/`user`
    claims, sent RAW (no 'Bearer '). Other Meltwater APIs send the SAME token as
    'Bearer <jwt>' — so we strip that and reuse it. This lets us capture the
    right token from ANY Meltwater request, not just the tagging BFF."""
    if not auth:
        return None
    tok = auth[7:] if auth.lower().startswith("bearer ") else auth
    parts = tok.split(".")
    if len(parts) != 3:
        return None
    try:
        pad = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(pad))
    except Exception:
        return None
    return tok if ("company" in payload or "user" in payload) else None


async def _discover_search_id(page, base):
    """Find a saved-search id from the Explore list so we can open its results
    feed (the only view that calls the tagging BFF). Any search works — the
    token we grab there is account-scoped, not search-scoped. Returns a string
    id or None."""
    import re
    try:
        await page.goto(f"{base}/a/explore/list", wait_until="domcontentloaded")
    except Exception:
        pass
    for _ in range(15):
        try:
            html = await page.content()
        except Exception:
            html = ""
        m = re.search(r"searchId[=\"':\s]+(\d{5,})", html) or re.search(r"/results\?searchId=(\d+)", html)
        if m:
            return m.group(1)
        # also check the current URL in case the app auto-redirected into a feed
        try:
            m2 = re.search(r"searchId=(\d+)", page.url)
            if m2:
                return m2.group(1)
        except Exception:
            pass
        await asyncio.sleep(1.0)
    return None


async def apply_results(email, password, results, request_otp=None, throttle_s=0.4,
                        saved_state=None, on_state_captured=None, environment=None):
    """Apply Bentley tags to Meltwater by Document ID. Returns a report dict:
    {applied, failed, total, unmapped, message, failures}.

    Session reuse (SSO): if `saved_state` (a Playwright storage_state JSON
    string) is given, it's loaded into the browser first so a still-valid
    session skips the whole SSO + SMS-OTP login — that flow alone takes
    ~60-90s, and is the single biggest cause of a slow apply. If the saved
    session is expired, we do NOT auto-prompt OTP (same product rule as the
    sentiment brands' apply) — we return `_session_expired` so the caller can
    ask the analyst to clear it. On a fresh login (no saved_state), the
    resulting session is captured via `on_state_captured(state_json)` so later
    runs need no OTP until it's cleared."""
    tag_map = aa.resolve_tag_map()
    manifest, unmapped = aa.manifest_from_results(results, tag_map)
    if not manifest:
        return {"applied": 0, "failed": 0, "total": 0, "unmapped": sorted(unmapped),
                "message": "Nothing to apply (no document ids or mappable tags)."}

    captured = {"auth": None}
    ok, failed = [], []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=config.HEADLESS, args=CHROMIUM_LAUNCH_ARGS)
        ctx_kwargs = {}
        if saved_state:
            try:
                ctx_kwargs["storage_state"] = json.loads(saved_state)
            except Exception as e:
                log.warning("bentley apply: saved session couldn't be parsed (%s) — ignoring it", e)
        ctx = await _new_browser_context(browser, **ctx_kwargs)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        # Grab the token ONLY from the tagging BFF (bff.fhaicoreapps.com). The app
        # sends a DIFFERENT token to other services (e.g. feature-toggle) that the
        # tagging endpoint accepts (202) but silently ignores — only the token the
        # BFF itself receives actually writes tags. Confirmed by capture: the
        # working manual tag used the fhaicoreapps token, not the feature-toggle one.
        async def _grab(req):
            if captured["auth"]:
                return
            try:
                if "fhaicoreapps.com" not in req.url:
                    return
                h = await req.all_headers()
                tok = _tag_token(h.get("authorization"))
                if tok:
                    captured["auth"] = tok
                    captured["src"] = req.url.split("?")[0]
            except Exception:
                pass
        ctx.on("request", _grab)

        # Response listener: harvest a real searchId from Meltwater's discovery
        # graphql. The account's saved searches (e.g. "Bentley Global Coverage")
        # come back in this response even when /a/explore doesn't auto-open a
        # feed — the ids live in the JSON, NOT the rendered HTML (which is why
        # scraping the page for a searchId found nothing). We then open that
        # search's results feed, which is what fires the tagging BFF.
        found = {"search_id": None}
        _bentley_re = re.compile(r'"id":"(\d{6,})"[^{}]{0,200}?"name":"([^"]*[Bb]entley[^"]*)"')
        _anysearch_re = re.compile(r'"__typename":"Search"[^{}]{0,300}?"id":"(\d{6,})"')

        async def _grab_search(resp):
            if found["search_id"]:
                return
            try:
                u = resp.url
                if "meltwater.io" not in u or "graphql" not in u:
                    return
                body = await resp.text()
                m = _bentley_re.search(body) or _anysearch_re.search(body)
                if m:
                    found["search_id"] = m.group(1)
                    log.info("bentley apply: discovered a saved searchId=%s from discovery graphql",
                             found["search_id"])
            except Exception:
                pass
        ctx.on("response", _grab_search)

        if saved_state:
            log.info("bentley apply: SESSION REUSE — trying the saved Meltwater session (no OTP)…")
            if await _is_logged_in(page):
                log.info("bentley apply: SESSION REUSE — saved session is VALID; skipping login/OTP ✓")
            else:
                # Expired/invalid. Per the product rule, do NOT auto-prompt OTP
                # while a saved session exists — ask the user to clear it.
                log.warning("bentley apply: SESSION REUSE — saved session is EXPIRED/invalid; "
                            "NOT prompting OTP. User must clear it on Profile to log in fresh.")
                await browser.close()
                return {"applied": 0, "failed": len(manifest), "total": len(manifest),
                        "_session_expired": True,
                        "message": ("Your saved Meltwater session has expired. Go to Profile → "
                                     "'Log out of Meltwater / clear saved session', then run Apply "
                                     "again to log in once (you'll enter an SMS code that one time).")}
        else:
            log.info("bentley apply: logging in %s (docs=%d)", email, len(manifest))
            try:
                login_ok, msg = await login_to_meltwater(page, email, password, request_otp)
            except Exception as e:
                await browser.close()
                return {"applied": 0, "failed": len(manifest), "total": len(manifest),
                        "message": f"Meltwater login errored: {type(e).__name__}: {e}"}
            if not login_ok:
                await browser.close()
                return {"applied": 0, "failed": len(manifest), "total": len(manifest),
                        "message": f"Meltwater login failed: {msg}"}
            # Capture the fresh logged-in session so future runs skip OTP.
            if on_state_captured is not None:
                try:
                    state = await ctx.storage_state()
                    on_state_captured(json.dumps(state))
                    log.info("bentley apply: SESSION SAVE — captured the logged-in session for "
                             "reuse (future runs won't need OTP until it's cleared) ✓")
                except Exception as e:
                    log.warning("bentley apply: SESSION SAVE — could not capture the session: %s", e)

        # SSO logins (@meltwater.com) land on a PERSONAL workspace that doesn't
        # own the brand's documents/tags and has no search feed to open — so the
        # tagging token can never be captured there. Switch into the brand's
        # Meltwater account/workspace first (same step the sentiment apply uses),
        # so the feed we open below is the one that owns the documents we're
        # tagging. `environment` is the account name to switch into (set per brand
        # in Brand Studio). If it's not set, we skip the switch and try the
        # current workspace (works for single-workspace accounts).
        if environment:
            switched = await switch_meltwater_account(page, environment)
            if switched:
                log.info("bentley apply: switched into the '%s' workspace ✓", environment)
            else:
                log.warning("bentley apply: could NOT switch into the '%s' workspace — the tagging "
                            "token capture will likely fail. Check the brand's Environment matches a "
                            "Meltwater account name exactly.", environment)
        else:
            log.warning("bentley apply: no Environment configured for this brand — staying in the "
                        "login's default workspace. If token capture fails, set the brand's "
                        "Environment to the Meltwater account that owns the Bentley coverage.")

        # The tagging BFF (bff.fhaicoreapps.com) is only called from a search's
        # RESULTS feed — NOT the home page or the saved-search LIST the app lands
        # on after login. So we must open an actual results feed to make that call
        # fire, then capture its token (account-scoped, so ANY search works).
        # We try, in order, and log every step so a failure is never silent:
        #   1) /a/explore — the app usually redirects to the last/default search's
        #      results feed, which fires the tagging call without needing a searchId
        #   2) fallback: discover a searchId from /a/explore/list and open its feed
        base = config.MELTWATER_URL.rstrip("/").split("/a/")[0]

        async def _wait_token(seconds):
            for _ in range(seconds):
                if captured["auth"]:
                    return True
                await asyncio.sleep(1.0)
            return bool(captured["auth"])

        log.info("bentley apply: capturing tagging token — opening /a/explore …")
        try:
            await page.goto(f"{base}/a/explore", wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            log.warning("bentley apply: /a/explore navigation issue: %s: %s", type(e).__name__, e)

        # Opening /a/explore also makes the app fetch the account's saved searches
        # over graphql — our response listener harvests a searchId from that. Wait
        # a short while for either the token (if a feed auto-opened) OR a searchId.
        search_id = None
        for _ in range(15):
            if captured["auth"] or found["search_id"]:
                break
            await asyncio.sleep(1.0)

        if captured["auth"]:
            log.info("bentley apply: token captured from the default Explore feed ✓")
        else:
            # Open a REAL saved search's results feed — the only reliable way to
            # make the app call the tagging BFF. Prefer the searchId harvested
            # from graphql; fall back to scraping the list page.
            search_id = found["search_id"] or await _discover_search_id(page, base)
            log.info("bentley apply: opening a saved search feed; searchId=%s", search_id)
            if search_id:
                feed_url = f"{base}/a/explore/results?searchId={search_id}"
                log.info("bentley apply: opening results feed %s", feed_url)
                try:
                    await page.goto(feed_url, wait_until="domcontentloaded", timeout=30000)
                except Exception as e:
                    log.warning("bentley apply: results-feed navigation issue: %s: %s",
                                type(e).__name__, e)
                got = await _wait_token(25)
                if not got:
                    # Re-nudge the feed once — the heavy content stream can be slow.
                    log.info("bentley apply: no token after opening the feed; re-opening once…")
                    try:
                        await page.goto(feed_url, wait_until="domcontentloaded", timeout=30000)
                    except Exception:
                        pass
                    await _wait_token(25)

        token = captured["auth"]
        if not token:
            await browser.close()
            log.warning("bentley apply: NO TOKEN CAPTURED — never observed a bff.fhaicoreapps.com "
                        "call. searchId=%s. The results feed may not have rendered in time.", search_id)
            return {"applied": 0, "failed": len(manifest), "total": len(manifest),
                    "message": ("Logged into Meltwater, but couldn't capture the tagging token — "
                                "no search results feed opened in this account's workspace. Open any "
                                "saved search once in Meltwater (so it becomes the default view), then "
                                "run Apply again.")}
        log.info("bentley apply: TOKEN OK — proceeding to tag %d document(s)", len(manifest))

        log.info("bentley apply: captured auth (len %d) from %s", len(token), captured.get("src"))
        headers = {"authorization": token, "content-type": "application/json",
                   "origin": "https://app.meltwater.com", "referer": "https://app.meltwater.com/"}

        # Resolve tag names to IDs against THIS account's LIVE tag list only.
        # The bundled tag_ids.json was captured from a DIFFERENT Meltwater account,
        # so its ids do not exist here — Meltwater 202-accepts them but silently
        # drops them (the tag never appears). So when we can read the live tags for
        # the account we've switched into, we resolve against those ONLY, and any
        # name not found here is reported as `unmapped` rather than sent with a
        # wrong-account id. Only if the live fetch fails do we fall back to the
        # bundled manifest (best-effort).
        try:
            r = await ctx.request.get(aa.TAGS_URL, headers={"authorization": token})
            live_map = aa.tag_map_from_tags_json(await r.json()) if r.ok else {}
        except Exception:
            live_map = {}
        if live_map:
            m2, u2 = aa.manifest_from_results(results, live_map)   # live account tags ONLY
            manifest, unmapped = m2, u2
            log.info("bentley apply: resolved against %d live account tags; docs=%d unmapped=%s",
                     len(live_map), len(manifest), sorted(unmapped))
            if unmapped:
                log.warning("bentley apply: %d tag name(s) not found in this Meltwater account "
                            "(NOT applied — would be a wrong-account id): %s",
                            len(unmapped), sorted(unmapped))
            if not manifest:
                await browser.close()
                return {"applied": 0, "failed": 0, "total": 0, "unmapped": sorted(unmapped),
                        "message": ("None of the classified tags exist in this Meltwater account "
                                    "(Bentley Systems). The tag names may differ here — see the "
                                    "unmapped list in the logs.")}
        else:
            log.warning("bentley apply: could not read the account's live tag list — falling back to "
                        "the bundled map (ids may be from a different account)")

        sample_error = None
        first_response = None
        for i, m in enumerate(manifest):
            body = aa.build_body(m)
            try:
                resp = await ctx.request.post(aa.ENDPOINT, headers=headers, data=json.dumps(body))
                if first_response is None:
                    try:
                        rtext = (await resp.text())[:500]
                    except Exception:
                        rtext = ""
                    first_response = f"status {resp.status}: {rtext}"
                    log.info("bentley apply: FIRST RESPONSE status=%s body=%s | sent body=%s",
                             resp.status, rtext, json.dumps(body)[:400])
                if resp.ok:
                    ok.append(m["document_id"])
                    log.info("bentley apply: [%d/%d] OK status=%s doc=%s tagIds=%s",
                             i + 1, len(manifest), resp.status, m["document_id"], m["tag_ids"])
                else:
                    failed.append((m.get("url", m["document_id"]), resp.status))
                    if sample_error is None:
                        try:
                            btext = (await resp.text())[:200]
                        except Exception:
                            btext = ""
                        sample_error = f"status {resp.status}: {btext}"
                        log.warning("bentley apply: first failure -> %s | doc=%s tagIds=%s",
                                    sample_error, m["document_id"], m["tag_ids"])
            except Exception as e:
                failed.append((m.get("url", m["document_id"]), str(e)))
                if sample_error is None:
                    sample_error = f"{type(e).__name__}: {e}"
                    log.warning("bentley apply: first failure (exception) -> %s", sample_error)
            await asyncio.sleep(max(0.0, throttle_s))  # throttle the shared account
        await browser.close()

    log.info("bentley apply done: applied=%d failed=%d (email=%s) sample_error=%s",
             len(ok), len(failed), email, sample_error)
    return {"applied": len(ok), "failed": len(failed), "total": len(manifest),
            "unmapped": sorted(unmapped), "message": "ok",
            "sample_error": sample_error, "first_response": first_response,
            "failures": [f"{u} (status {s})" for u, s in failed[:15]]}
