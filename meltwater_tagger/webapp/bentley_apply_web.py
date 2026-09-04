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

from playwright.async_api import async_playwright

import config
from logging_setup import get_logger
from meltwater_apply import login_to_meltwater, _new_browser_context, _is_logged_in, CHROMIUM_LAUNCH_ARGS
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
                        saved_state=None, on_state_captured=None):
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

        # The tagging BFF (bff.fhaicoreapps.com) is only called from a search's
        # RESULTS feed — NOT the home page or the saved-search LIST the app lands
        # on after login. So we open an actual results feed to make that call fire,
        # then capture its token (which is account-scoped, so ANY search works).
        # We discover a searchId from the saved-search list, then open its feed.
        # (Do NOT reload in a tight loop — that interrupts the app before it can
        # make the call, which is what failed before.)
        base = config.MELTWATER_URL.rstrip("/").split("/a/")[0]
        search_id = await _discover_search_id(page, base)
        feed_url = f"{base}/a/explore/results?searchId={search_id}" if search_id else None
        for i in range(30):
            if captured["auth"]:
                break
            if feed_url and i in (2, 12):
                try:
                    await page.goto(feed_url, wait_until="domcontentloaded")
                except Exception:
                    pass
            await asyncio.sleep(1.0)
        token = captured["auth"]
        if not token:
            await browser.close()
            return {"applied": 0, "failed": len(manifest), "total": len(manifest),
                    "message": "Logged in, but could not capture the Meltwater tagging token."}

        log.info("bentley apply: captured auth (len %d) from %s", len(token), captured.get("src"))
        headers = {"authorization": token, "content-type": "application/json",
                   "origin": "https://app.meltwater.com", "referer": "https://app.meltwater.com/"}

        # Self-healing tag IDs: pull Meltwater's CURRENT tag list and let it
        # override the bundled tag_ids.json, so tags the client added/renamed in
        # Meltwater still resolve WITHOUT a code change. If this fetch fails, we
        # keep the manifest already built from the bundled map (original behavior).
        try:
            r = await ctx.request.get(aa.TAGS_URL, headers={"authorization": token})
            live_map = aa.tag_map_from_tags_json(await r.json()) if r.ok else {}
        except Exception:
            live_map = {}
        if live_map:
            merged = dict(tag_map)
            merged.update(live_map)          # live Meltwater ids win
            m2, u2 = aa.manifest_from_results(results, merged)
            if m2:
                manifest, unmapped = m2, u2
                log.info("bentley apply: merged %d live Meltwater tags; manifest=%d unmapped=%d",
                         len(live_map), len(manifest), len(u2))

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
