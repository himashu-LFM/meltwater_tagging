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
import json
import os
import re
import sys

import httpx

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_THIS_DIR))  # project root (apply_tags, config)
sys.path.insert(0, _THIS_DIR)  # webapp/ itself (logging_setup), for standalone import
from playwright.async_api import async_playwright

from apply_tags import (
    SELECTORS, norm_permalink, get_card_permalink, card_existing_tag,
    expand_similar_if_needed, apply_tag_to_card, normalize_tag,
    build_post_fallback, resolve_target, reddit_post_id, log_card_links,
)
import config
from logging_setup import get_logger

log = get_logger("meltwater_apply")

MELTWATER_LOGIN_URL = os.environ.get("MELTWATER_LOGIN_URL", "https://app.meltwater.com/login")

# Auth0 SPA SDK's Local Storage cache key. The key format is the same for
# every user of this Meltwater tenant (client id + audience + scope are fixed
# by Meltwater's own Auth0 app config) — only the cached VALUE differs per
# user/session. Override via env if Meltwater ever changes their Auth0 client.
AUTH0_STORAGE_KEY = os.environ.get(
    "MELTWATER_AUTH0_STORAGE_KEY",
    "@@auth0spajs@@::sy6sQF2zJZWJd1jqupARpRuUIEl9xyH6::"
    "https://authorize.meltwater.com/api/v2::openid profile email offline_access",
)


def decode_session_expiry(storage_value: str):
    """Best-effort: pull the `exp` claim out of the cached access_token's JWT
    payload, for a status indicator. Returns a unix timestamp, or None if the
    value can't be parsed (doesn't raise — this is purely informational)."""
    import base64
    import json as _json
    try:
        outer = _json.loads(storage_value)
        access_token = outer.get("body", {}).get("access_token") or outer.get("access_token")
        payload_b64 = access_token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        payload = _json.loads(base64.urlsafe_b64decode(payload_b64))
        return payload.get("exp")
    except Exception:
        return None

# Confirmed against the real Meltwater login page (Auth0-hosted login,
# single-page form -- email + password both present together, no separate
# "continue" step). Primary selectors are exact; the broader fallbacks after
# the comma keep this working if Meltwater ever changes their Auth0 theme.
LOGIN_SELECTORS = {
    "email": '#email, input[name="email"], input[type="email"], input[name="username"]',
    "password": '#password, input[name="password"], input[type="password"]',
    "submit": (
        'button._button-login-password, button[type="submit"], input[type="submit"], '
        'button:has-text("Log in"), button:has-text("Log In"), button:has-text("Login"), '
        'button:has-text("Sign in"), button:has-text("Sign In"), '
        'button:has-text("Continue"), button:has-text("Next")'
    ),
}


# --- Microsoft Entra SSO (for @meltwater.com internal accounts) --------------
# Meltwater-internal accounts (…@meltwater.com) are federated to Microsoft Entra
# / Azure AD. After the FIRST Meltwater email screen (identical to the normal
# flow), the browser is redirected to login.microsoftonline.com and walks:
#   MS email -> MS password -> MFA "Approve request" -> "I can't use my
#   Authenticator" -> "Verify your identity" chooser -> Text (SMS) -> enter code.
# The SMS code can't be read server-side, so login_via_microsoft_sso pauses and
# asks for it through a `request_otp` callback the web app wires to a popup on
# the tagging screen. Any non-meltwater.com email keeps the existing Auth0 flow.
#
# Selectors are Microsoft's standard Entra login element IDs (stable across
# tenants); the text fallbacks keep them working if Microsoft re-themes.
MS_SSO_HOST = "login.microsoftonline.com"

MS_SSO_SELECTORS = {
    "email": 'input[type="email"], input[name="loginfmt"], #i0116',
    "password": 'input[type="password"], input[name="passwd"], #i0118',
    "submit": '#idSIButton9, input[type="submit"], button[type="submit"]',
    # "I can't use my Microsoft Authenticator app right now"
    "cant_use_authenticator": (
        '#signInAnotherWay, a#signInAnotherWay, '
        'a:has-text("I can\'t use my Microsoft Authenticator"), '
        'a:has-text("different method"), a:has-text("another way")'
    ),
    "otp_field": 'input[name="otc"], #idTxtBx_SAOTCC_OTC, input[type="tel"], input[type="text"]',
    "otp_verify": '#idSubmit_SAOTCC_Continue, #idSIButton9, input[type="submit"]',
    "otp_error": '#idSpan_SAOTCC_Error_OTC, .alert-error, [role="alert"]',
    # "Stay signed in?" (KMSI) prompt shown right after a successful OTP. Either
    # button dismisses it; we click "Yes" (#idSIButton9) so the session persists
    # and the redirect back to Meltwater fires immediately.
    "kmsi_yes": (
        '#idSIButton9, input[type="submit"][value="Yes"], '
        'button:has-text("Yes")'
    ),
    "kmsi_no": '#idBtn_Back, input[type="button"][value="No"], button:has-text("No")',
}


# When true (default), the SSO step logs the exact email / password / OTP being
# entered, so you can follow along in the backend logs. WARNING: these are
# plaintext secrets in stdout — set MELTWATER_LOG_SECRETS=false to turn them off
# once you're done debugging.
_LOG_SECRETS = os.environ.get("MELTWATER_LOG_SECRETS", "true").lower() == "true"


def is_meltwater_sso_email(email: str) -> bool:
    """@meltwater.com accounts log in through Microsoft Entra SSO; everyone else
    (ListenFirstMedia etc.) uses the standard Auth0 flow, unchanged."""
    return bool(email) and email.strip().lower().endswith("@meltwater.com")


async def _ms_fill_and_next(page, selector, value, label):
    """Fill a Microsoft Entra field, blur it, and advance (button click, else
    Enter). Microsoft enables the Next/Sign in button only after a blur, same as
    the Auth0 form."""
    field = await page.wait_for_selector(selector, timeout=20000)
    await field.fill(value)
    await field.press("Tab")
    await page.wait_for_timeout(400)
    btn = await page.query_selector(MS_SSO_SELECTORS["submit"])
    if btn:
        await btn.click()
    else:
        await field.press("Enter")
    log.info("login[ms-sso]: submitted %s", label)


async def login_via_microsoft_sso(page, email, password, request_otp) -> tuple[bool, str]:
    """Drive the Meltwater -> Microsoft Entra SSO login for @meltwater.com
    accounts, up to and INCLUDING entering the SMS OTP. Post-OTP navigation
    (the "Stay signed in?" prompt and reaching the topic feed) is intentionally
    not implemented yet.

    request_otp: async callback (attempt, max_attempts, error) -> code|None,
    supplied by the caller (the web app). Without it MFA can't be completed."""
    log.info("login[ms-sso]: ===== Microsoft SSO flow START for %s =====", email)

    # Screen 1 — Meltwater's own email box (identical to the standard flow).
    log.info("login[ms-sso]: STEP 1/6 — Meltwater login page: opening %s", MELTWATER_LOGIN_URL)
    try:
        await page.goto(MELTWATER_LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
    except Exception as e:
        log.error("login[ms-sso]: STEP 1/6 FAILED — could not open the Meltwater login page: %s", e)
        return False, f"Could not open the Meltwater login page: {e}"
    try:
        mw_email = await page.wait_for_selector(LOGIN_SELECTORS["email"], timeout=15000)
    except Exception:
        log.error("login[ms-sso]: STEP 1/6 FAILED — Meltwater email field not found %s", await _diag(page))
        return False, f"Meltwater email field not found {await _diag(page)}"
    if _LOG_SECRETS:
        log.info("login[ms-sso]: STEP 1/6 — typing email %r into the Meltwater box and clicking Next", email)
    else:
        log.info("login[ms-sso]: STEP 1/6 — typing the email into the Meltwater box and clicking Next")
    await mw_email.fill(email)
    await mw_email.press("Tab")
    await page.wait_for_timeout(500)
    await _submit_step(page, mw_email, "mw-email")

    # Redirect to Microsoft Entra.
    log.info("login[ms-sso]: STEP 2/6 — waiting for redirect to Microsoft (login.microsoftonline.com)")
    try:
        await page.wait_for_url(f"**{MS_SSO_HOST}**", timeout=30000)
        log.info("login[ms-sso]: STEP 2/6 — now on Microsoft: %s", page.url)
    except Exception:
        # Some tenants reach the MS screen without a URL change we catch;
        # fall through and look for the field directly.
        log.info("login[ms-sso]: STEP 2/6 — no Microsoft URL match yet %s", await _diag(page))

    # Screen 2 — Microsoft usually goes straight to the password screen for this
    # account (the Meltwater email carries over). We do NOT require a separate MS
    # email entry: only fill it if an email field actually appears quickly (same
    # Meltwater address); otherwise proceed straight to the password.
    log.info("login[ms-sso]: STEP 2/6 — checking for a Microsoft email screen (optional)")
    try:
        ms_email = await page.wait_for_selector(MS_SSO_SELECTORS["email"], timeout=4000)
    except Exception:
        ms_email = None
    if ms_email:
        if _LOG_SECRETS:
            log.info("login[ms-sso]: STEP 2/6 — Microsoft email screen shown; re-entering email %r and clicking Next", email)
        else:
            log.info("login[ms-sso]: STEP 2/6 — Microsoft email screen shown; re-entering the Meltwater email and clicking Next")
        await ms_email.fill(email)
        await ms_email.press("Tab")
        await page.wait_for_timeout(400)
        btn = await page.query_selector(MS_SSO_SELECTORS["submit"])
        if btn:
            await btn.click()
        else:
            await ms_email.press("Enter")
        try:
            await page.wait_for_load_state("networkidle", timeout=20000)
        except Exception:
            pass
    else:
        log.info("login[ms-sso]: STEP 2/6 — no Microsoft email screen; going straight to the password")

    # Screen 3 — Microsoft password box.
    if _LOG_SECRETS:
        log.info("login[ms-sso]: STEP 3/6 — Microsoft password page: typing password %r and clicking Sign in", password)
    else:
        log.info("login[ms-sso]: STEP 3/6 — Microsoft password page: typing the password and clicking Sign in")
    try:
        await _ms_fill_and_next(page, MS_SSO_SELECTORS["password"], password, "ms-password")
    except Exception:
        log.error("login[ms-sso]: STEP 3/6 FAILED — Microsoft password field never appeared %s", await _diag(page))
        return False, f"Microsoft password field never appeared {await _diag(page)}"
    try:
        await page.wait_for_load_state("networkidle", timeout=20000)
    except Exception:
        pass

    # Screen 4 — MFA "Approve sign in request": we can't approve a push, so pick
    # "I can't use my Microsoft Authenticator app right now" to reach the method
    # chooser. (Absent on single-method tenants that jump straight to a code.)
    log.info("login[ms-sso]: STEP 4/6 — MFA screen: clicking \"I can't use my Microsoft Authenticator app right now\"")
    try:
        link = await page.wait_for_selector(MS_SSO_SELECTORS["cant_use_authenticator"], timeout=15000)
        await link.click()
        log.info("login[ms-sso]: STEP 4/6 — chose an alternate verification method")
        await page.wait_for_timeout(1500)
    except Exception:
        log.info("login[ms-sso]: STEP 4/6 — no 'can't use authenticator' link shown (skipping) %s", await _diag(page))

    # Screen 5 — "Verify your identity" chooser: click the Text (SMS) option.
    log.info("login[ms-sso]: STEP 5/6 — 'Verify your identity' chooser: clicking the Text (SMS) option")
    clicked_text = False
    try:
        text_opt = page.get_by_text(re.compile(r"^\s*Text\b", re.I))
        if await text_opt.count() > 0:
            await text_opt.first.click()
            clicked_text = True
    except Exception:
        pass
    if not clicked_text:
        try:
            node = await page.query_selector(
                'div[role="button"]:has-text("Text"), div[data-value]:has-text("Text")')
            if node:
                await node.click()
                clicked_text = True
        except Exception:
            pass
    if clicked_text:
        log.info("login[ms-sso]: STEP 5/6 — SMS code requested; Microsoft is texting the code to the phone")
        await page.wait_for_timeout(1500)
    else:
        log.info("login[ms-sso]: STEP 5/6 — no Text option shown (may already be on the code screen) %s",
                  await _diag(page))

    # Screen 6 — OTP entry. Ask the analyst for the code, up to 3 attempts.
    if request_otp is None:
        log.error("login[ms-sso]: STEP 6/6 FAILED — an SMS code is required but no OTP input was wired up")
        return False, ("This @meltwater.com account needs an SMS verification code, but there "
                       "was no way to enter one (OTP entry is only available in the web app).")

    max_attempts = 3
    last_error = None
    for attempt in range(1, max_attempts + 1):
        log.info("login[ms-sso]: STEP 6/6 — waiting for the analyst to enter the SMS code (attempt %d/%d)",
                  attempt, max_attempts)
        code = await request_otp(attempt, max_attempts, last_error)
        if not code:
            log.warning("login[ms-sso]: STEP 6/6 — no code entered in time; giving up")
            return False, "No verification code was entered in time (waited up to 5 minutes)."
        code = str(code).strip()
        if _LOG_SECRETS:
            log.info("login[ms-sso]: STEP 6/6 — got code %r; typing it on the Microsoft screen and submitting (attempt %d)", code, attempt)
        else:
            log.info("login[ms-sso]: STEP 6/6 — got a code; typing it on the Microsoft screen and submitting (attempt %d)", attempt)
        try:
            otp_field = await page.wait_for_selector(MS_SSO_SELECTORS["otp_field"], timeout=15000)
            await otp_field.fill(code)
            await otp_field.press("Tab")
            await page.wait_for_timeout(300)
            verify = await page.query_selector(MS_SSO_SELECTORS["otp_verify"])
            if verify:
                await verify.click()
            else:
                await otp_field.press("Enter")
        except Exception as e:
            last_error = "Could not enter the code on the Microsoft page — try again."
            log.warning("login[ms-sso]: STEP 6/6 — OTP entry failed (attempt %d): %s", attempt, e)
            continue

        await page.wait_for_timeout(3000)
        try:
            await page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass

        err = None
        try:
            err_el = await page.query_selector(MS_SSO_SELECTORS["otp_error"])
            if err_el and await err_el.is_visible():
                err = (await err_el.inner_text()).strip()
        except Exception:
            pass
        if err:
            last_error = err or "That code wasn't accepted — check it and try again."
            log.warning("login[ms-sso]: STEP 6/6 — code rejected on attempt %d: %s", attempt, last_error)
            continue

        # No visible error -> the code was accepted.
        log.info("login[ms-sso]: STEP 6/6 — code accepted on attempt %d %s", attempt, await _diag(page))
        # Finish the flow: dismiss "Stay signed in?" and wait for the redirect
        # back to app.meltwater.com so callers land on an authenticated app page.
        return await _finish_ms_sso(page)

    log.error("login[ms-sso]: STEP 6/6 FAILED — the code was rejected %d times", max_attempts)
    return False, f"The verification code was rejected {max_attempts} times."


async def _finish_ms_sso(page) -> tuple[bool, str]:
    """Post-OTP navigation for the Microsoft Entra SSO flow.

    After a correct OTP, Microsoft shows a "Stay signed in?" (KMSI) prompt and
    then redirects back to Meltwater (authorize.meltwater.com -> app.meltwater.com).
    This dismisses the KMSI prompt (clicking "Yes" so the session persists) and
    waits until the browser is actually back on app.meltwater.com and off any
    login/authorize screen — otherwise the caller's next step (account switch /
    opening the topic feed) would run against a Microsoft interstitial.

    Also dismisses Meltwater's own "Create a passkey" interstitial if it appears
    on the way in, mirroring the Auth0 flow."""
    # STEP 7 — "Stay signed in?" (KMSI). Optional; absent on some tenants.
    log.info("login[ms-sso]: STEP 7 — handling the 'Stay signed in?' prompt (if shown)")
    try:
        kmsi = await page.wait_for_selector(MS_SSO_SELECTORS["kmsi_yes"], timeout=8000)
        await kmsi.click()
        log.info("login[ms-sso]: STEP 7 — clicked 'Yes' on 'Stay signed in?'")
    except Exception:
        log.info("login[ms-sso]: STEP 7 — no 'Stay signed in?' prompt shown (skipping)")

    # STEP 8 — wait for the redirect chain back to Meltwater to settle.
    log.info("login[ms-sso]: STEP 8 — waiting for redirect back to app.meltwater.com")
    try:
        await page.wait_for_url("**app.meltwater.com**", timeout=45000)
    except Exception:
        # Some redirects don't fire a matchable URL event; fall back to polling.
        log.info("login[ms-sso]: STEP 8 — no direct URL match; polling for the Meltwater app")
    try:
        await page.wait_for_load_state("networkidle", timeout=30000)
    except Exception:
        pass

    # Poll a few seconds: the login/authorize hosts must have dropped away and we
    # must be on app.meltwater.com before we call the login a success.
    landed = False
    for _ in range(10):
        url = (page.url or "").lower()
        on_app = "app.meltwater.com" in url
        still_auth = any(h in url for h in ("login.microsoftonline.com",
                                            "authorize.meltwater.com")) or "/login" in url
        if on_app and not still_auth:
            landed = True
            break
        await page.wait_for_timeout(1500)

    # Meltwater's post-login "Create a passkey" interstitial (same as Auth0 flow).
    try:
        skip = await page.wait_for_selector(
            'button:has-text("Continue without passkeys"), a:has-text("Continue without passkeys")',
            timeout=5000,
        )
        await skip.click()
        log.info("login[ms-sso]: STEP 8 — dismissed the passkey-enrollment prompt")
        try:
            await page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
    except Exception:
        log.debug("login[ms-sso]: STEP 8 — no passkey prompt shown (or already past it)")

    if not landed:
        log.error("login[ms-sso]: STEP 8 FAILED — never reached the Meltwater app after OTP %s",
                   await _diag(page))
        return False, (
            "Signed in to Microsoft, but the browser never landed back on the Meltwater "
            f"app after the code {await _diag(page)}. The 'Stay signed in?' step or the "
            "redirect back to Meltwater may have changed."
        )
    log.info("login[ms-sso]: STEP 8 — landed on the Meltwater app %s", await _diag(page))
    log.info("login[ms-sso]: ===== Microsoft SSO flow DONE =====")
    return True, "ok"


# Result-card container selector. Broadened beyond the original guess; the
# real one is confirmed via the feed diagnostic below and can be pinned via env.
# Confirmed live: Meltwater's feed is a Virtuoso virtualized list; each result
# card is a direct child of [data-testid="virtuoso-item-list"].
CARD_SELECTOR = os.environ.get(
    "MELTWATER_CARD_SELECTOR",
    '[data-testid="virtuoso-item-list"] > div, [data-index], [data-item-index]',
)

# Chromium launch flags for containerized/cloud hosts (Render, Docker).
#  --disable-dev-shm-usage: the container's default 64MB /dev/shm is exhausted by
#    a heavy SPA like Meltwater -> the page renders BLANK (bodyTextLen=0) even
#    though navigation "succeeds". This writes to /tmp instead. (Main Render fix.)
#  --no-sandbox / --disable-setuid-sandbox: required because most container
#    runtimes run as a user that can't use Chromium's sandbox.
#  --disable-blink-features=AutomationControlled: drops the navigator.webdriver
#    automation flag that enterprise SPAs use to serve a blank/blocked page to
#    bots. Combined with a real user-agent + viewport (see _new_browser_context),
#    this makes the headless run look like an ordinary desktop Chrome.
#  --window-size: give the SPA a real desktop viewport so it lays out normally.
# All harmless locally; essential on Render.
CHROMIUM_LAUNCH_ARGS = [
    "--disable-dev-shm-usage",
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-gpu",
    "--disable-blink-features=AutomationControlled",
    "--window-size=1440,900",
    # --- memory reduction for small cloud instances -----------------------
    # Rendering Meltwater's heavy SPA in headless Chrome can OOM a small Render
    # instance, which Render handles by KILLING AND RESTARTING the whole
    # container mid-request (surfaces to the browser as a 502). These flags cut
    # Chromium's peak memory substantially:
    #  - site-per-process / IsolateOrigins off: don't spawn a separate renderer
    #    process per origin (the single biggest saver for a multi-origin SPA)
    #  - background/extension/GPU subsystems disabled: fewer helper processes
    #  - cap the V8 heap so a runaway script can't balloon past the RAM limit
    "--disable-features=site-per-process,TranslateUI,IsolateOrigins",
    "--disable-extensions",
    "--disable-background-networking",
    "--disable-renderer-backgrounding",
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--renderer-process-limit=1",
    "--js-flags=--max-old-space-size=512",
]

# Resource types that are pure weight for our purpose. Tagging needs the
# mentions LIST — text, links, and the tag buttons — none of which are images,
# video, fonts, or the analytics charts/maps. Aborting these cuts Chromium's
# peak RAM (decoded images + font atlases are among the largest consumers), CPU,
# and network round-trips, which is what lets the results view finish rendering
# on a small instance where the full page otherwise stalls. CSS is kept so the
# feed layout / hover toolbars still work.
_BLOCKED_RESOURCE_TYPES = {"image", "media", "font"}

# Third-party trackers / session-recorders / chat widgets / product-tour tools
# that Meltwater's app loads but that are pure overhead for tagging. FullStory
# records the entire DOM continuously, Intercom loads a whole chat app, Pendo
# pulls many guide-tour files, the rest are analytics/RUM/surveys. Confirmed
# from a live network capture; none touch the mentions feed or the tag modal, so
# aborting them cuts RAM/CPU with zero functional risk. Matched as substrings of
# the request URL. (Deliberately does NOT include any *.meltwater.io/.com data
# endpoints — those serve the feed/tags and must load.)
_BLOCKED_HOSTS = (
    "fullstory.com",
    "intercom.io",
    "intercom.com",
    "pendo.io",
    "pendo-static",
    "segment.io",
    "segment.com",
    "go-mpulse.net",
    "satismeter.com",
    "doubleclick.net",
    "google-analytics.com",
    "googletagmanager.com",
    # Meltwater's own extras that tagging never needs and that are safe to drop:
    "_vercel/insights",                 # Vercel web-analytics beacon
    "meltwater.com/a/mira",             # Mira AI-companion route prefetch
    "sas-web-api.notifications",        # notification counts/list
)

# Endpoints whose full request (headers + body) and response body we capture, to
# see exactly what auth/headers/payload a direct httpx call would need to search
# mentions, list tags, and apply a tag — the basis for a browser-free apply.
_CAPTURE_PATTERNS = (
    "enqueue-document-tagging",   # the call that actually applies a tag
    "content-stream-bff",         # any content-stream-bff path (tags, and maybe expand)
    "/msearch",                   # analytics/volume search
    "discovery-next",             # discovery graphql
    "masfsearch",                 # masf search — candidate for the "expand group" fetch
)

# Full (untruncated) captures are also written here so we don't rely on scrolling
# the terminal — the file can be read directly. Overwritten at the start of each
# apply run so it only holds the latest.
_CAPTURE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mw_capture.log")


def _redact_headers(hdrs: dict) -> dict:
    """Strip secret header VALUES (auth token, cookies, api keys) before writing
    a capture to disk, keeping only the scheme prefix + length so the code can be
    built without ever recording the live credential."""
    out = {}
    for k, v in (hdrs or {}).items():
        lk = k.lower()
        if lk in ("authorization", "cookie", "x-api-key", "apikey") or "token" in lk or "secret" in lk:
            if isinstance(v, str) and v.lower().startswith("bearer "):
                out[k] = f"Bearer <redacted len={len(v) - 7}>"
            else:
                out[k] = f"<redacted len={len(v) if isinstance(v, str) else '?'}>"
        else:
            out[k] = v
    return out


def _capture_write(text: str) -> None:
    try:
        with open(_CAPTURE_FILE, "a", encoding="utf-8") as f:
            f.write(text.rstrip() + "\n\n" + ("-" * 80) + "\n\n")
    except Exception:
        pass


async def _reset_capture_file():
    try:
        open(_CAPTURE_FILE, "w", encoding="utf-8").close()
    except Exception:
        pass


async def _new_browser_context(browser, **kwargs):
    await _reset_capture_file()
    """A browser context that presents as an ordinary desktop Chrome, not a
    headless bot. Meltwater (like many enterprise SPAs) can serve a blank page
    to an obvious automation client, which shows up as bodyTextLen=0. A real
    user-agent + viewport + hiding navigator.webdriver avoids that. Extra
    new_context kwargs (e.g. none today) pass straight through."""
    context = await browser.new_context(
        user_agent=config.BROWSER_UA,
        viewport={"width": 1440, "height": 900},
        locale="en-US",
        **kwargs,
    )
    # Mask the most common automation tell before any page script runs.
    await context.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
    )

    # Abort heavy, tagging-irrelevant resources (images/media/fonts) to cut
    # Chromium's RAM/CPU/network footprint on a small instance. This is the main
    # code-only lever for making the results view render where it otherwise
    # stalls/OOMs. Kept resilient: any error falls back to letting the request
    # through, so blocking can never wedge a request.
    logged_endpoints = set()

    async def _route(route):
        req = route.request
        rtype = req.resource_type
        url = req.url
        try:
            if rtype in _BLOCKED_RESOURCE_TYPES:
                await route.abort()
                return
        except Exception:
            pass
        # Abort pure-overhead third-party trackers (FullStory/Intercom/Pendo/etc).
        if any(h in url for h in _BLOCKED_HOSTS):
            try:
                await route.abort()
                return
            except Exception:
                pass
        # Log each unique XHR/fetch endpoint (host+path, no query) so we can see
        # which API calls feed the mentions LIST vs the heavy analytics / AI-
        # insight panes — then block the latter to render only what tagging needs.
        if rtype in ("xhr", "fetch", "document"):
            try:
                from urllib.parse import urlsplit
                u = urlsplit(req.url)
                key = f"{u.netloc}{u.path}"
                if key not in logged_endpoints:
                    logged_endpoints.add(key)
                    log.info("apply[net]: %s %s", req.method, key)
            except Exception:
                pass
        # Full request capture for the tagging-relevant endpoints (headers + body)
        # so we can see exactly what a direct httpx call would have to send.
        if any(p in url for p in _CAPTURE_PATTERNS):
            try:
                hdrs = await req.all_headers()
                # redact nothing here — this is the user's own token in their own
                # local log; they need it to replicate the call. Truncate size.
                log.info("apply[capture-req]: %s %s (full body -> %s)",
                          req.method, url, _CAPTURE_FILE)
                _capture_write(f"REQUEST {req.method} {url}\nheaders={_redact_headers(hdrs)}\nbody={req.post_data or ''}")
            except Exception as e:
                log.warning("apply[capture-req] failed: %s", e)
        try:
            await route.continue_()
        except Exception:
            pass

    await context.route("**/*", _route)

    # Capture the RESPONSE bodies for the same endpoints — this is where the
    # msearch results (document_ids + source URLs) and the tags list come back.
    async def _on_response(resp):
        try:
            if any(p in resp.url for p in _CAPTURE_PATTERNS):
                body = await resp.text()
                log.info("apply[capture-resp]: %s %s (full body -> %s)",
                          resp.status, resp.url, _CAPTURE_FILE)
                _capture_write(f"RESPONSE {resp.status} {resp.url}\nbody={body}")
        except Exception:
            pass

    context.on("response", _on_response)
    return context


def _container_memory():
    """(used_mb, limit_mb) for the whole container via cgroup — the number Render
    checks against the memory limit, and it INCLUDES the Chromium child
    processes (the real hogs), not just this Python process. Falls back to this
    process's RSS, then to (None, None) if nothing is readable."""
    # cgroup v2 (modern hosts)
    for used_path, limit_path in (
        ("/sys/fs/cgroup/memory.current", "/sys/fs/cgroup/memory.max"),
        ("/sys/fs/cgroup/memory/memory.usage_in_bytes",
         "/sys/fs/cgroup/memory/memory.limit_in_bytes"),
    ):
        try:
            with open(used_path) as f:
                used = int(f.read().strip())
            limit = None
            try:
                with open(limit_path) as f:
                    v = f.read().strip()
                    if v != "max":
                        limit = int(v)
                        if limit > (1 << 62):  # v1 "unlimited" sentinel
                            limit = None
            except Exception:
                pass
            return used / 1048576.0, (limit / 1048576.0 if limit else None)
        except Exception:
            continue
    # fallback: this process only (excludes Chromium children)
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024.0, None
    except Exception:
        pass
    return None, None


def _mem_str():
    used, limit = _container_memory()
    if used is None:
        return "mem=?"
    if limit:
        return f"mem={used:.0f}/{limit:.0f}MB ({100.0 * used / limit:.0f}%)"
    return f"mem={used:.0f}MB"


async def _wait_for_feed_and_diagnose(page):
    """Wait for the results feed to render, and if no cards are found, dump the
    DOM's data-testid inventory + a sample of links so we can identify the real
    card selector without a screenshot."""
    # give the SPA time + wait for any plausible card to appear
    try:
        await page.wait_for_load_state("networkidle", timeout=25000)
    except Exception:
        log.debug("feed: networkidle wait timed out (continuing)")
    try:
        await page.wait_for_selector(CARD_SELECTOR, timeout=25000)
        n = len(await page.query_selector_all(CARD_SELECTOR))
        log.info("feed: card selector matched %d element(s)", n)
        if n > 0:
            return n
    except Exception:
        log.warning("feed: no cards matched CARD_SELECTOR within timeout")

    # Nothing matched -> dump diagnostics about what IS on the page.
    try:
        info = await page.evaluate("""() => {
            const counts = {};
            document.querySelectorAll('[data-testid]').forEach(el => {
                const t = el.getAttribute('data-testid');
                counts[t] = (counts[t] || 0) + 1;
            });
            const top = Object.entries(counts).sort((a,b)=>b[1]-a[1]).slice(0, 25);
            const articleCount = document.querySelectorAll('article').length;
            const redditLinks = document.querySelectorAll('a[href*="reddit.com"]').length;
            const openArticle = document.querySelectorAll('[aria-label*="Open article"]').length;
            return { top_testids: top, articleCount, redditLinks, openArticle,
                     url: location.href, bodyLen: document.body.innerText.length };
        }""")
        log.warning("feed diagnostic: url=%s bodyTextLen=%s articles=%s redditLinks=%s "
                     "openArticleIcons=%s", info.get("url"), info.get("bodyLen"),
                     info.get("articleCount"), info.get("redditLinks"), info.get("openArticle"))
        log.warning("feed diagnostic: top data-testid values (name x count) -> %s",
                     info.get("top_testids"))
    except Exception as e:
        log.warning("feed diagnostic failed: %s: %s", type(e).__name__, e)
    return 0


# Controls that switch Explore from the analytics/summary view to the actual
# mentions list. Meltwater sometimes lands on the summary dashboard (KPI / top
# sources / geo / term cards) where there are NO result cards to tag.
MENTIONS_TAB_RE = "mentions|documents|results|content|coverage|articles|feed"


async def _ensure_mentions_view(page) -> int:
    """If the mentions feed isn't showing, try to switch to it.

    Explore can open on the Summary/analytics view instead of the mentions
    list. Detect that (no result cards) and click a Mentions/Documents/Results
    tab to reveal the list. Always logs the tab/nav candidates so the exact
    control can be pinned if the guess is wrong. Returns the resulting card
    count."""
    import re as _re
    try:
        n = len(await page.query_selector_all(CARD_SELECTOR))
    except Exception:
        n = 0
    if n > 0:
        return n

    # Diagnostic: list clickable tabs/buttons so we can identify the real one.
    try:
        cands = await page.evaluate("""() => {
            const out = [];
            document.querySelectorAll('button,[role="tab"],a[href],[data-testid]').forEach(el => {
                const t = (el.innerText || '').trim().slice(0, 30);
                const tid = el.getAttribute('data-testid');
                const role = el.getAttribute('role');
                if (t || tid) out.push({t, tid, role});
            });
            return out.slice(0, 80);
        }""")
        log.info("apply: mentions-view candidates -> %s", cands)
    except Exception:
        pass

    name_re = _re.compile(MENTIONS_TAB_RE, _re.I)
    for getter in ("tab", "button", "link"):
        try:
            loc = page.get_by_role(getter, name=name_re)
            if await loc.count() > 0:
                await loc.first.click()
                log.info("apply: clicked a '%s' control matching the mentions view", getter)
                await page.wait_for_timeout(3000)
                break
        except Exception as e:
            log.debug("apply: mentions-view %s click failed: %s", getter, e)
    # last resort: a data-testid that looks like a mentions/documents list tab
    try:
        el = await page.query_selector(
            '[data-testid*="mentions" i],[data-testid*="documents" i],[data-testid*="results-tab" i]'
        )
        if el:
            await el.click()
            log.info("apply: clicked a mentions/documents data-testid control")
            await page.wait_for_timeout(3000)
    except Exception:
        pass

    try:
        return len(await page.query_selector_all(CARD_SELECTOR))
    except Exception:
        return 0


async def _log_frames(page, label: str):
    """Log every frame's URL. If the login form is inside an iframe, our
    page-level selectors won't see it -- this reveals that."""
    try:
        frames = page.frames
        info = " | ".join(f"[{i}]{f.url[:80]}" for i, f in enumerate(frames))
        log.info("login[%s]: %d frame(s) -- %s", label, len(frames), info)
    except Exception as e:
        log.warning("login[%s]: could not enumerate frames: %s", label, e)


async def _submit_step(page, field, label: str) -> str:
    """Advance a login step as robustly as possible, independent of the exact
    button markup. Tries, in order: role-based button click (matches <button>,
    <a role=button>, <div role=button>, <input type=submit>), our CSS selector
    fallback, then pressing Enter in the given field. Returns which method fired."""
    import re as _re
    name_re = _re.compile(r"(next|log\s*in|login|sign\s*in|continue|submit)", _re.I)

    # 1) role-based (pierces past tag-name assumptions)
    try:
        loc = page.get_by_role("button", name=name_re)
        n = await loc.count()
        for i in range(n):
            item = loc.nth(i)
            if await item.is_visible() and await item.is_enabled():
                await item.click()
                return f"role-button[{i}]"
    except Exception as e:
        log.debug("login[%s]: role-button attempt failed: %s", label, e)

    # 2) CSS selector fallback
    try:
        el = await page.query_selector(LOGIN_SELECTORS["submit"])
        if el and await el.is_visible():
            await el.click()
            return "css-selector"
    except Exception as e:
        log.debug("login[%s]: css-selector attempt failed: %s", label, e)

    # 3) Enter key in the field (most markup-independent)
    try:
        await field.press("Enter")
        return "enter-key"
    except Exception as e:
        log.debug("login[%s]: enter-key attempt failed: %s", label, e)

    return "none"


async def _log_submit_candidates(page, label: str):
    """Log every element our broad submit selector matches, in DOM order, so
    we can tell if we're about to click the right button or an unrelated decoy
    (hidden buttons, consent banners, etc. that happen to match the fallback
    text patterns)."""
    try:
        els = await page.query_selector_all(LOGIN_SELECTORS["submit"])
        if not els:
            log.warning("login[%s]: submit selector matched NOTHING", label)
            return
        parts = []
        for i, el in enumerate(els):
            try:
                tag = await el.evaluate("e => e.tagName")
                text = (await el.inner_text()).strip().replace("\n", " ")[:40]
                visible = await el.is_visible()
                enabled = await el.is_enabled()
            except Exception:
                tag, text, visible, enabled = "?", "?", "?", "?"
            parts.append(f"[{i}]<{tag} visible={visible} enabled={enabled}>{text!r}")
        log.info("login[%s]: submit selector matched %d element(s) -- %s",
                  label, len(els), " | ".join(parts))
    except Exception as e:
        log.warning("login[%s]: could not enumerate submit candidates: %s", label, e)


async def _diag(page) -> str:
    """Best-effort page state for debugging a failed login — url, title, and a
    short snippet of VISIBLE TEXT ONLY (not full HTML/DOM, so it can't leak
    input values like a typed password). Reveals things like a validation
    error, a bot-check, or an unexpected screen without needing a screenshot."""
    try:
        title = await page.title()
    except Exception:
        title = "?"
    snippet = ""
    try:
        text = await page.locator("body").inner_text(timeout=2000)
        snippet = " ".join(text.split())[:220]
    except Exception:
        pass
    return f"(page url: {page.url} | title: {title!r} | visible text: {snippet!r})"


async def login_to_meltwater(page, email: str, password: str, request_otp=None) -> tuple[bool, str]:
    # @meltwater.com internal accounts federate to Microsoft Entra SSO — a
    # different flow (MS email -> MS password -> MFA -> SMS OTP). Everything
    # else (ListenFirstMedia etc.) uses the standard Auth0 flow below, unchanged.
    if is_meltwater_sso_email(email):
        return await login_via_microsoft_sso(page, email, password, request_otp)
    log.info("login: opening %s", MELTWATER_LOGIN_URL)
    try:
        await page.goto(MELTWATER_LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
    except Exception as e:
        log.error("login: failed to open login page: %s: %s", type(e).__name__, e)
        return False, f"Could not open the Meltwater login page: {e}"

    log.info("login: looking for email field")
    try:
        email_field = await page.wait_for_selector(LOGIN_SELECTORS["email"], timeout=15000)
    except Exception:
        log.error("login: email field not found %s", await _diag(page))
        return False, (
            "Could not find an email/username field on the Meltwater login page "
            f"{await _diag(page)}. The login form structure differs from our guess — "
            "share this page's HTML (or a screenshot) so the selectors can be fixed."
        )
    await email_field.fill(email)
    # Some forms only enable the "Next"/"Log in" button after the field loses
    # focus (a blur event), not just on typing -- fill() dispatches input
    # events but not always blur. Force it so the button isn't left disabled.
    await email_field.press("Tab")
    # Give the form's own client-side validation JS a moment to run and enable
    # the button -- our fill+blur happens far faster than a human would type,
    # which can outrace a debounced validator.
    await page.wait_for_timeout(500)
    log.info("login: email filled")

    # Some flows require submitting the email first before a password field appears
    # (Auth0/Okta-style "continue" step); others show both fields at once.
    pwd_field = await page.query_selector(LOGIN_SELECTORS["password"])
    if not pwd_field:
        log.info("login: password field not immediately present — advancing to password step")
        await _log_frames(page, "step1-next")
        await _log_submit_candidates(page, "step1-next")
        url_before = page.url
        method = await _submit_step(page, email_field, "step1-next")
        log.info("login: advanced step1 via %s (url before=%s, immediately after=%s)",
                  method, url_before, page.url)

        # Screen 1 (app.meltwater.com/login) does a full cross-origin navigation
        # to authorize.meltwater.com for the password step -- wait for that.
        try:
            await page.wait_for_load_state("networkidle", timeout=20000)
        except Exception:
            log.debug("login: networkidle wait after step1 timed out (continuing anyway)")
        try:
            pwd_field = await page.wait_for_selector(LOGIN_SELECTORS["password"], timeout=20000)
        except Exception:
            log.error("login: password field never appeared (advance method=%s) %s",
                       method, await _diag(page))
            await _log_frames(page, "step1-fail")
            return False, (
                f"After entering the email, the password step never loaded (tried: {method}). "
                f"{await _diag(page)}. The 'Next' button may be a non-standard element or inside "
                "an iframe — the frame log will show which."
            )

    if not pwd_field:
        log.error("login: no password field found %s", await _diag(page))
        return False, f"No password field found on the login page {await _diag(page)}."

    await pwd_field.fill(password)
    await pwd_field.press("Tab")  # same blur-triggering safeguard as the email field
    await page.wait_for_timeout(500)
    log.info("login: password filled")

    await _log_frames(page, "step2-login")
    await _log_submit_candidates(page, "step2-login")
    url_before = page.url
    method = await _submit_step(page, pwd_field, "step2-login")
    log.info("login: submitted password via %s (url before=%s, immediately after=%s)",
              method, url_before, page.url)

    try:
        await page.wait_for_load_state("networkidle", timeout=30000)
    except Exception:
        log.warning("login: networkidle wait timed out (continuing anyway)")

    # Meltwater shows a passkey-enrollment interstitial after a successful
    # login ("Create a passkey" / "Continue without passkeys"). The session is
    # already authenticated at this point, but leaving this screen up can
    # interfere with the next navigation, so dismiss it if present.
    try:
        skip = await page.wait_for_selector(
            'button:has-text("Continue without passkeys"), a:has-text("Continue without passkeys")',
            timeout=5000,
        )
        await skip.click()
        log.info("login: dismissed passkey-enrollment prompt")
        await page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        log.debug("login: no passkey prompt shown (or already past it)")

    if "login" in page.url.lower():
        log.error("login: still on login page after submit %s", await _diag(page))
        return False, f"Still on the login page after submit {await _diag(page)} — check credentials or selectors."
    log.info("login: success %s", await _diag(page))
    return True, "ok"


async def _scroll_feed(page):
    """Scroll Meltwater's Virtuoso results list. Its scroll happens on an
    internal overflow container, not the window, so page.mouse.wheel is a no-op.
    We find the actual scroller (the Virtuoso scroller, or the nearest scrollable
    ancestor of the item list) and scrollBy on it."""
    try:
        moved = await page.evaluate("""() => {
            const pick = () => {
                let s = document.querySelector('[data-testid="virtuoso-scroller"]');
                if (s) return s;
                const list = document.querySelector('[data-testid="virtuoso-item-list"]');
                let el = list;
                while (el && el !== document.body) {
                    const oy = getComputedStyle(el).overflowY;
                    if ((oy === 'auto' || oy === 'scroll') && el.scrollHeight > el.clientHeight + 5) return el;
                    el = el.parentElement;
                }
                return document.scrollingElement || document.documentElement;
            };
            const s = pick();
            const before = s.scrollTop;
            s.scrollBy(0, Math.max(400, s.clientHeight * 0.8));
            return { before, after: s.scrollTop, height: s.scrollHeight,
                     client: s.clientHeight, tag: s.tagName,
                     testid: s.getAttribute && s.getAttribute('data-testid') };
        }""")
        log.debug("apply: scrolled feed %s", moved)
        return moved
    except Exception as e:
        log.debug("apply: scroll failed (%s) — falling back to mouse wheel", e)
        try:
            await page.mouse.wheel(0, 3000)
        except Exception:
            pass
    return None


async def _scroll_to_top(page):
    """Reset the Virtuoso scroller to the top so a fresh top-to-bottom pass sees
    the whole feed again (used between the exact-match and Similar-expansion
    passes)."""
    try:
        await page.evaluate("""() => {
            const s = document.querySelector('[data-testid="virtuoso-scroller"]')
                || document.scrollingElement || document.documentElement;
            if (s) s.scrollTop = 0;
        }""")
        await asyncio.sleep(1.0)
    except Exception as e:
        log.debug("apply: scroll-to-top failed (%s) — continuing", e)


# Every individually-taggable mention (a top-level post AND each expanded
# "Similar" sub-post) has its own "Open article in new tab" control. We use that
# as the per-mention anchor: the smallest ancestor of a post link that contains
# exactly one such control IS that mention's own card — which is the element we
# must hover + tag. This is what stops a similar sub-post from being tagged on
# its parent.
OPEN_ARTICLE_SEL = '[aria-label*="Open article in new tab" i]'


async def _candidate_cards(page, include_nested: bool):
    """Elements to consider this round.

    - Not expanded: the top-level virtualized list items, as-is.
    - Expanded ("Similar" groups open): one element PER MENTION, derived by
      iterating every "Open article in new tab" control (there is exactly one
      per taggable mention) and climbing to the largest ancestor that still
      owns only THAT control. This yields the parent's own card AND each
      similar sub-post's own card as separate elements.

      Crucially, each card is stamped with `data-mw-open-href` = the href of ITS
      OWN open-article control. That is this mention's true source URL. Keying a
      sub-post on its own control (instead of "deepest reddit link anywhere in
      the card") is what stops several comments of the SAME thread — which all
      contain the same post-level links — from collapsing onto one post-level
      key and leaving the real sub-post unmatched."""
    top = list(await page.query_selector_all(CARD_SELECTOR))
    if not include_nested:
        return top

    try:
        arr = await page.evaluate_handle(
            """(OPEN) => {
                const seen = new Set();
                const cards = [];
                document.querySelectorAll(OPEN).forEach(ctrl => {
                    // climb to this mention's card: largest ancestor still
                    // wrapping exactly THIS one open-article control (its body
                    // plus the hover toolbar where the Tag icon lives) — never
                    // the group container, which owns 2+ such controls.
                    let card = ctrl, p = ctrl.parentElement;
                    while (p && p !== document.body &&
                           p.querySelectorAll(OPEN).length === 1) {
                        card = p; p = p.parentElement;
                    }
                    if (!card || card === document.body || seen.has(card)) return;
                    seen.add(card);
                    // This mention's OWN source url comes from ITS control
                    // specifically. The control may be the <a> itself, sit inside
                    // one, or wrap one.
                    const a = (ctrl.matches && ctrl.matches('a[href]')) ? ctrl
                        : ((ctrl.closest && ctrl.closest('a[href]'))
                           || (ctrl.querySelector && ctrl.querySelector('a[href]')));
                    let href = a ? (a.getAttribute('href') || '') : '';
                    // The open-article control often resolves to a Meltwater
                    // wrapper/redirect link, NOT the post's real source URL.
                    // Only stamp a genuine source URL; otherwise leave it unset
                    // so _card_key falls back to the deepest reddit link in the
                    // (now tightly-scoped) per-mention card.
                    if (href && href.indexOf('meltwater') !== -1) href = '';
                    if (href) card.setAttribute('data-mw-open-href', href);
                    else card.removeAttribute('data-mw-open-href');
                    cards.push(card);
                });
                return cards;
            }""",
            OPEN_ARTICLE_SEL,
        )
        props = await arr.get_properties()
        cards = []
        for _, h in props.items():
            el = h.as_element()
            if el is not None:
                cards.append(el)
        await arr.dispose()
        if cards:
            log.debug("apply[expand]: derived %d per-mention card(s) from open-article controls", len(cards))
            return cards
    except Exception as e:
        log.debug("apply[expand]: per-mention derivation failed (%s) — falling back to list items", e)
    return top


async def _dump_reddit_links(page, to_apply, handled):
    """Definitive diagnostic: after expanding Similar groups, list every reddit
    link present anywhere on the page and report whether each still-missing
    target's id tokens (post id + comment id) appear at all. If a target's
    comment id is absent from every link, Meltwater simply isn't exposing that
    sub-post's own URL in the DOM (so no key-matching change can reach it)."""
    missing = [k for k in to_apply if k not in handled]
    if not missing:
        return
    want = []
    for k in missing:
        want.extend(t for t in k.replace("reddit:", "").split("/") if t)
    try:
        links = await page.evaluate(
            """() => [...document.querySelectorAll('a[href]')]
                .map(a => a.getAttribute('href'))
                .filter(h => h && h.toLowerCase().indexOf('reddit.com') !== -1)"""
        )
    except Exception as e:
        log.warning("apply[diag]: could not collect reddit links: %s", e)
        return
    uniq = sorted(set(links or []))
    log.info("apply[diag]: %d reddit link(s) on expanded page; want id token(s)=%s",
             len(uniq), want)
    for h in uniq[:60]:
        log.info("apply[diag]: reddit link -> %s", h)
    present = {tok: any(tok in (h or "") for h in (links or [])) for tok in want}
    log.info("apply[diag]: target id token present-on-page -> %s", present)


async def _card_key(card):
    """Canonical match key for a candidate card.

    Prefer the mention's OWN open-article href stamped on the element by
    `_candidate_cards` (so same-thread comments don't collapse to one post-level
    key). Fall back to the deepest reddit link in the card for top-level cards,
    which aren't stamped."""
    href = None
    try:
        href = await card.get_attribute("data-mw-open-href")
    except Exception:
        href = None
    if href:
        key = norm_permalink(href)
        if key:
            return key
    return await get_card_permalink(card)


async def _tag_matched_card(page, card, target_key, val, delay,
                            applied, skipped_already, failed, handled) -> None:
    """Apply the tag to a card already matched to a target: honour an existing
    tag (skip, never override), otherwise tag it, and record the outcome. On a
    transient/stale-element error the target is left un-handled so a later round
    retries it."""
    tag = val["tag"]
    orig = val["orig"]
    try:
        # Read the card's OWN visible tags first — a genuinely-applied tag shows
        # a visible "Remove [tag]" chip.
        existing = None
        try:
            existing = await card_existing_tag(card)
        except Exception:
            pass
        if existing:
            log.info("apply: %s already tagged (%s) — skipping", orig, existing)
            skipped_already.append({"permalink": orig, "existing_tags": existing})
            handled.add(target_key)
            return

        ok = await apply_tag_to_card(page, card, tag, dry_run=False, delay=delay)
        if ok:
            log.info("apply: tagged %s -> %s", orig, tag)
            applied.append({"permalink": orig, "tag": tag})
        else:
            log.warning("apply: could not tag %s -> %s (see [card-buttons]/[tag-modal] logs)",
                         orig, tag)
            failed.append({"permalink": orig, "tag": tag})
        handled.add(target_key)
    except Exception as e:
        # Stale/detached element mid-action -> don't mark handled, retry later.
        log.warning("apply: transient error on %s (%s: %s) — will retry",
                     orig, type(e).__name__, e)


async def _scan_feed(page, to_apply, handled, delay, applied, skipped_already, failed,
                     *, expand_similar, use_fallback, post_fallback, similar_pids):
    """One disciplined top-to-bottom pass over the virtualized feed.

    - expand_similar: open every collapsed "Similar articles" group in view
      before scanning, so its sub-posts render and become matchable.
    - use_fallback: allow the Reddit post-id fallback (comment<->post
      granularity). When False, only an EXACT permalink match tags a card.

    Records into `similar_pids` the post-id of every card that carries a
    Similar group, so the caller can keep the post-id fallback from ever tagging
    the parent of a grouped post."""
    seen_any = set()
    no_new_rounds = 0
    bottom_rounds = 0   # consecutive rounds the scroller physically could not advance
    rounds = 0
    MAX_ROUNDS = 200
    label = "expand" if expand_similar else ("fallback" if use_fallback else "exact")
    # Post-ids we're looking for, so the expand pass can log which group members
    # it actually surfaces (this is how we confirm a nested comment sub-post is
    # being keyed at comment granularity, not collapsed to its parent post).
    target_pids = {reddit_post_id(k) for k in to_apply}
    target_pids.discard("")
    logged_group_keys = set()
    # Terminate on reaching the BOTTOM of the feed, not on "no new posts for a
    # few rounds". The expand pass keeps extending the scroll height as it opens
    # Similar groups on the way down, so a no-new-posts heuristic quits far too
    # early (it stopped ~27% down, before reaching groups lower in the feed).
    while rounds < MAX_ROUNDS and len(handled) < len(to_apply):
        rounds += 1
        before_seen = len(seen_any)

        if expand_similar:
            # Only open a Similar group whose PARENT shares a post-id with a
            # still-missing target. The target comment is the parent post's URL
            # with /comment/<id> appended, so it can only live inside that one
            # group — opening every unrelated group just churns the DOM and
            # risks mis-tagging. (When there are no reddit target post-ids, open
            # all, preserving the original behaviour.)
            # The collapsed-state selector never matches an already-open group,
            # so clicking a match can only OPEN a group, never re-collapse one.
            want_csv = ",".join(sorted(target_pids))
            toggles = await page.query_selector_all(SELECTORS["similar_collapsed"])
            opened = 0
            for t in toggles:
                try:
                    relevant = await t.evaluate(
                        """(node, wantCsv) => {
                            const want = new Set(wantCsv ? wantCsv.split(',') : []);
                            if (want.size === 0) return true;  // no reddit targets -> open all
                            // climb to the nearest ancestor that actually has
                            // reddit links (the parent card), and match its post-id
                            let b = node;
                            while (b && b !== document.body) {
                                const links = [...b.querySelectorAll('a[href]')]
                                    .map(a => a.getAttribute('href') || '')
                                    .filter(h => h.toLowerCase().indexOf('reddit.com') !== -1);
                                if (links.length) {
                                    for (const h of links) {
                                        const m = h.match(/\\/comments\\/([a-z0-9]+)/i);
                                        if (m && want.has(m[1].toLowerCase())) return true;
                                    }
                                    return false;  // card has reddit links, none match a target
                                }
                                b = b.parentElement;
                            }
                            return false;
                        }""",
                        want_csv,
                    )
                    if not relevant:
                        continue
                    await t.click()
                    await asyncio.sleep(0.4)
                    opened += 1
                except Exception:
                    pass
            if opened:
                log.info("apply[expand]: opened %d Similar group(s) matching a target post-id", opened)

        cards = await _candidate_cards(page, include_nested=expand_similar)
        for card in cards:
            # Note Similar-group parents (before they're expanded) so the
            # post-id fallback can never tag the parent of a grouped post.
            try:
                has_similar = await card.query_selector(SELECTORS["similar_collapsed"])
            except Exception:
                has_similar = None
            try:
                permalink = await _card_key(card)
            except Exception:
                continue  # detached mid-read; ignore, it'll re-render
            if not permalink:
                continue
            seen_any.add(permalink)
            pid = reddit_post_id(permalink)
            if has_similar and pid:
                similar_pids.add(pid)
            # Diagnostic: when expanding, surface every card whose post-id matches
            # a target's, so we can see whether the specific comment sub-post
            # (e.g. reddit:<post>/<comment>) is being keyed at comment
            # granularity or only at post level.
            if expand_similar and pid in target_pids and permalink not in logged_group_keys:
                logged_group_keys.add(permalink)
                log.info("apply[expand]: group member for target post %s -> key=%s", pid, permalink)

            target_key, val = resolve_target(
                permalink, to_apply, post_fallback if use_fallback else {})
            if target_key is None or target_key in handled:
                continue
            if target_key != permalink:
                log.info("apply: card %s matched target %s via post-id fallback",
                          permalink, target_key)
            if expand_similar:
                # Confirm WHICH element we're about to tag (its own links should
                # be just this sub-post's — if they include a different post's,
                # we resolved to the wrong container and the log will show it).
                try:
                    desc = await card.evaluate(
                        """el => ({
                            tag: el.tagName,
                            testid: el.getAttribute('data-testid'),
                            cls: (el.className || '').toString().slice(0, 60),
                            links: [...el.querySelectorAll('a[href]')]
                                .map(a => a.getAttribute('href'))
                                .filter(h => h && h.indexOf('meltwater') === -1).slice(0, 6),
                            hasTagIcon: !!el.querySelector('[aria-label=\"Tag\"],[title=\"Tag\"],'
                                + '[data-testid=\"LocalOfferIcon\"],[data-testid=\"SellIcon\"]'),
                        })""")
                    log.info("apply[expand]: tagging element for %s -> %s | element=%s",
                              val["orig"], target_key, desc)
                except Exception:
                    pass
            await _tag_matched_card(page, card, target_key, val, delay,
                                    applied, skipped_already, failed, handled)
            if len(handled) >= len(to_apply):
                break

        moved = await _scroll_feed(page)
        await asyncio.sleep(1.2)
        if len(seen_any) == before_seen:
            no_new_rounds += 1
        else:
            no_new_rounds = 0
        # Bottom detection. `moved["after"] == moved["before"]` means the
        # scroller could not advance this round — i.e. we're at the bottom
        # (expanding a group re-opens headroom, so this only stays true once
        # every group has been expanded and the whole feed traversed). When the
        # scroll telemetry is unavailable (mouse-wheel fallback), fall back to
        # the old no-new-posts heuristic so we still terminate.
        if moved is None:
            at_bottom = no_new_rounds >= 4
        else:
            at_bottom = moved.get("after") == moved.get("before")
        bottom_rounds = bottom_rounds + 1 if at_bottom else 0
        try:
            top_cards = len(await page.query_selector_all(CARD_SELECTOR))
        except Exception:
            top_cards = -1
        log.info("apply[%s]: round %d — top_cards=%d seen=%d handled=%d/%d no_new=%d bottom=%d %s scroll=%s",
                  label, rounds, top_cards, len(seen_any), len(handled),
                  len(to_apply), no_new_rounds, bottom_rounds, _mem_str(), moved)
        # Two straight rounds pinned at the bottom = feed fully traversed.
        if bottom_rounds >= 2:
            break
    return seen_any


async def _walk_feed_and_tag(page, to_apply: dict) -> dict:
    """Shared feed-walking loop used by both the login-based and
    session-based apply paths. Assumes `page` is already on the topic feed
    and authenticated.

    `to_apply` maps a canonical permalink key -> {"tag", "orig"} where "orig" is
    the analyst's exact source URL (so the report can be matched back to the
    original results row on the frontend).

    Search order (mirrors how an analyst finds a post in Meltwater):
      1. EXACT URL match against the top-level feed.
      2. Anything still not found -> open each post's "Similar articles" group
         in turn and match the revealed sub-posts by exact URL. This is what
         fixes a similar sub-post being tagged on its PARENT instead.
      3. Only then, for posts with NO Similar group, the Reddit post-id fallback
         (comment<->post granularity) — never for a grouped post, so the parent
         of a Similar group is never tagged in place of the real sub-post."""
    applied, skipped_already, failed = [], [], []
    handled = set()   # target KEYS we've reached a FINAL decision on
    delay = config.ACTION_DELAY_MS / 1000.0
    post_fallback = build_post_fallback(to_apply)
    similar_pids = set()   # post-ids of cards that expose a "Similar" group

    # The feed is a heavy virtualized React app -- give it real time to render
    # results before we start scanning, and diagnose the DOM if nothing shows.
    n = await _wait_for_feed_and_diagnose(page)
    # Explore sometimes opens on the analytics/summary view (KPI / top-sources /
    # geo / term cards) or the mentions list simply hasn't populated yet. Either
    # way there are no result cards to tag. Try switching to the mentions view,
    # then a reload, before giving up — this is the difference between "the post
    # isn't here" and "we're on the wrong screen".
    attempts = 0
    while n == 0 and attempts < 3:
        attempts += 1
        n = await _ensure_mentions_view(page)
        if n > 0:
            log.info("apply: mentions view now shows %d card(s) after switch "
                      "(attempt %d)", n, attempts)
            break
        log.warning("apply: no result cards (attempt %d/3) — reloading the topic feed", attempts)
        try:
            await page.reload(wait_until="domcontentloaded", timeout=45000)
        except Exception as e:
            log.warning("apply: feed reload failed: %s: %s", type(e).__name__, e)
        n = await _wait_for_feed_and_diagnose(page)
    log.info("apply: feed load done — cards=%d %s", n, _mem_str())
    if n == 0:
        log.error("apply: results feed never rendered any cards — the page is not "
                   "showing the mentions list (see feed diagnostic + mentions-view "
                   "candidates above)")

    # --- STAGE 1: exact URL match against the top-level feed --------------------
    seen = await _scan_feed(
        page, to_apply, handled, delay, applied, skipped_already, failed,
        expand_similar=False, use_fallback=False,
        post_fallback=post_fallback, similar_pids=similar_pids)

    # --- STAGE 2: open each post's Similar group and match the sub-posts --------
    if len(handled) < len(to_apply):
        log.info("apply: %d target(s) not found at top level — opening Similar "
                  "groups and re-scanning", len(to_apply) - len(handled))
        await _scroll_to_top(page)
        seen |= await _scan_feed(
            page, to_apply, handled, delay, applied, skipped_already, failed,
            expand_similar=True, use_fallback=False,
            post_fallback=post_fallback, similar_pids=similar_pids)
        # Definitive check on what the expanded feed actually exposes for any
        # target we still couldn't match (case a: link present but mis-keyed,
        # vs case b: the sub-post's URL isn't in the DOM at all).
        await _dump_reddit_links(page, to_apply, handled)

    # --- STAGE 3: post-id fallback, but NEVER for a Similar-group post ----------
    if len(handled) < len(to_apply):
        safe_fallback = {pid: v for pid, v in post_fallback.items()
                         if pid not in similar_pids}
        if safe_fallback:
            log.info("apply: %d still missing — trying comment<->post fallback for "
                      "%d non-grouped post(s)", len(to_apply) - len(handled), len(safe_fallback))
            await _scroll_to_top(page)
            seen |= await _scan_feed(
                page, to_apply, handled, delay, applied, skipped_already, failed,
                expand_similar=False, use_fallback=True,
                post_fallback=safe_fallback, similar_pids=similar_pids)

    log.info("apply: scanned %d distinct posts", len(seen))
    unreached = [to_apply[k]["orig"] for k in to_apply if k not in handled]
    if unreached:
        log.warning("apply: %d target post(s) were never found in the feed: %s",
                     len(unreached), unreached[:5])
    log.info("apply: done — applied=%d failed=%d already=%d unreached=%d %s",
              len(applied), len(failed), len(skipped_already), len(unreached), _mem_str())
    return {
        "ok": True,
        "message": f"Applied {len(applied)} tag(s).",
        "applied": applied,
        "skipped_already": skipped_already,
        "failed": failed,
        "unreached": unreached,
    }


def _build_to_apply(results: list[dict]) -> dict:
    """Canonical-key -> {"tag", "orig"} map for every applyable result. `orig`
    keeps the analyst's exact source URL so the apply report can be matched back
    to the original results row (per-post 'Applied' status on the frontend)."""
    to_apply = {}
    for r in results:
        if r.get("action") == "apply" and r.get("tag") and r.get("permalink"):
            key = norm_permalink(r["permalink"])
            # First writer wins if two source rows canonicalize to the same key
            # (e.g. duplicate URLs) — they carry the same tag anyway.
            to_apply.setdefault(key, {"tag": r["tag"], "orig": r["permalink"]})
    return to_apply


def _check_apply_inputs(to_apply: dict, topic_url: str, require_topic: bool = True) -> dict | None:
    if not to_apply:
        log.warning("apply: nothing to apply — no results had action='apply' with a tag")
        return {"ok": False, "message": "No posts with an 'apply' action to tag.", "applied": [], "failed": []}
    # @meltwater.com (SSO) runs reach the feed via account switch + Advanced
    # search (by brand name), so they don't need a topic URL — only ListenFirst
    # (Auth0) runs do. Callers pass require_topic=False for the SSO path.
    if require_topic and not topic_url:
        log.error("apply: no topic_url given")
        return {"ok": False, "message": "This brand has no Meltwater topic URL configured yet (set it once in Brand settings).",
                "applied": [], "failed": []}
    return None


# =====================================================================================
# API-based apply (no feed rendering) — the memory-safe path.
#
# Instead of scrolling+clicking the heavy Explore feed (which OOMs a small
# instance), this reproduces exactly what the app does under the hood, using
# Meltwater's own internal endpoints (captured from the live app):
#   GET  bff.fhaicoreapps.com/prd-flux-content-stream-bff/tags            -> tag id by name
#   POST unified-search.meltwater.io/1.0/accounts/<acct>/msearch          -> url -> documentId
#   POST bff.fhaicoreapps.com/prd-flux-content-stream-bff/tags/enqueue-document-tagging
# The only browser work is a lightweight login (which renders fine in 512MB) plus
# a brief navigation to capture the session token + the exact msearch query the
# app builds for this saved search; then everything is plain httpx (~no memory).
# =====================================================================================

MSEARCH_HOST = "unified-search.meltwater.io"
BFF_TAGS_URL = "https://bff.fhaicoreapps.com/prd-flux-content-stream-bff/tags"
BFF_ENQUEUE_URL = "https://bff.fhaicoreapps.com/prd-flux-content-stream-bff/tags/enqueue-document-tagging"
_API_BASE_HEADERS = {
    "accept": "*/*",
    "accept-language": "en-US",
    "origin": "https://app.meltwater.com",
    "referer": "https://app.meltwater.com/",
    "user-agent": config.BROWSER_UA,
}


def _extract_search_id(topic_url: str) -> str | None:
    m = re.search(r"[?&]searchId=(\d+)", topic_url or "")
    return m.group(1) if m else None


def _iter_json_objects(text: str):
    """Yield each top-level JSON object from an msearch response, which is a
    stream of concatenated objects (one per batched sub-request), not a single
    JSON document."""
    dec = json.JSONDecoder()
    i, n = 0, len(text or "")
    while i < n:
        while i < n and text[i] in " \r\n\t":
            i += 1
        if i >= n:
            break
        try:
            obj, end = dec.raw_decode(text, i)
        except Exception:
            break
        yield obj
        i = end


def _expand_msearch_body(body_str: str, limit: int = 500) -> str:
    """Make the feed msearch return EVERY individual mention, including the ones
    Meltwater normally hides:
      * hiddenDocuments: "excluded" -> "included"  — the feed hides near-duplicate
        / "similar" mentions by default; those hidden docs ARE the similar
        sub-posts (e.g. a comment grouped under another). Including them is what
        surfaces the exact sub-post so we can tag it (never the parent).
      * pagination.limit bumped + start=0 + drop the 'similar' grouping — one flat
        page covering the whole window.
    """
    try:
        data = json.loads(body_str)
    except Exception:
        # best effort even if it isn't parseable as one object
        return body_str.replace('"hiddenDocuments":"excluded"', '"hiddenDocuments":"included"')

    stats = {"hidden": 0, "paged": 0}

    def walk(node):
        if isinstance(node, dict):
            if node.get("hiddenDocuments") == "excluded":
                node["hiddenDocuments"] = "included"
                stats["hidden"] += 1
            pag = node.get("pagination")
            if isinstance(pag, dict):
                pag["limit"] = limit
                pag["start"] = 0
                pag.pop("group", None)
                stats["paged"] += 1
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(data)
    log.info("apply-api: msearch tweaked (hiddenDocuments->included x%d, pages->limit=%d x%d)",
              stats["hidden"], limit, stats["paged"])
    return json.dumps(data)


def _hits_from_msearch(text: str) -> dict:
    """canonical-permalink-key -> {documentId, matchSentence, keywords}.

    Robust to response shape: recursively walks the whole response and collects
    EVERY object that has both a documentId and its own source url (flat feed
    hits, gyda-wrapped hits, grouped members, and AI-card citations all qualify).
    """
    out = {}

    def walk(node):
        if isinstance(node, dict):
            did = node.get("documentId")
            url = node.get("url") or node.get("originalUrl") or node.get("sourceUrl")
            if did and isinstance(url, str) and url.startswith("http"):
                key = norm_permalink(url)
                if key and key not in out:
                    out[key] = {
                        "documentId": did,
                        "matchSentence": node.get("matchSentence") or "",
                        "keywords": node.get("keywords") or [],
                    }
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    for obj in _iter_json_objects(text):
        try:
            walk(obj)
        except Exception:
            continue
    return out


async def _capture_session(email: str, password: str, topic_url: str, request_otp=None) -> dict:
    """Light browser step: log in, then briefly open the topic so the app fires
    its msearch — capturing the bearer token, the account id, and the exact
    msearch URL+body. Bails as soon as those are captured so the heavy feed
    render never completes (that render is what OOMs a small instance)."""
    cap = {"token": None, "account": None, "msearches": []}
    seen_bodies = set()
    got = asyncio.Event()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=CHROMIUM_LAUNCH_ARGS)
        context = await _new_browser_context(browser)

        def on_request(req):
            try:
                u = req.url
                if "meltwater" in u and not cap["token"]:
                    a = req.headers.get("authorization")
                    if a:
                        cap["token"] = a[7:] if a.lower().startswith("bearer ") else a
                if MSEARCH_HOST in u and req.method == "POST":
                    body = req.post_data or ""
                    if body and body not in seen_bodies:
                        seen_bodies.add(body)
                        m = re.search(r"/accounts/([^/]+)/msearch", u)
                        if m and not cap["account"]:
                            cap["account"] = m.group(1)
                        # Capture EVERY distinct msearch batch. The app fires
                        # several (feed, analytics, AI card); the member documents
                        # (similar sub-posts) come back in one of them, so we
                        # replay them all verbatim and merge — no need to know
                        # which, no query rewriting, no group expansion.
                        cap["msearches"].append({"url": u, "body": body})
                        got.set()
            except Exception:
                pass

        context.on("request", on_request)
        page = await context.new_page()
        ok, msg = await login_to_meltwater(page, email, password, request_otp)
        if not ok:
            await browser.close()
            return {"ok": False, "message": msg}

        try:
            await page.goto(topic_url, wait_until="commit", timeout=30000)
        except Exception:
            pass
        # Wait for the first msearch, then a short grace to collect the sibling
        # batches that fire together during initial load. We close before the
        # heavy results render finishes — memory-safe.
        try:
            await asyncio.wait_for(got.wait(), timeout=60)
        except Exception:
            log.warning("apply-api: no msearch observed within timeout")
        try:
            await asyncio.sleep(5)
        except Exception:
            pass
        await browser.close()

    if not (cap["token"] and cap["account"] and cap["msearches"]):
        return {"ok": False,
                "message": "Could not capture the Meltwater API session (token/msearch). "
                           "Falling back to the browser tagger."}
    cap["ok"] = True
    log.info("apply-api: captured %d msearch batch(es), account=%s",
              len(cap["msearches"]), cap["account"])
    return cap


async def apply_via_api(email: str, password: str, topic_url: str, results: list[dict],
                        request_otp=None) -> dict:
    """Memory-safe apply: capture the session in a light browser step, then tag
    every target via Meltwater's internal HTTP API (no feed rendering)."""
    to_apply = _build_to_apply(results)
    bad = _check_apply_inputs(to_apply, topic_url)
    if bad:
        return bad
    if not _extract_search_id(topic_url):
        return {"ok": False, "message": "Could not find searchId in the topic URL.",
                "applied": [], "failed": []}

    cap = await _capture_session(email, password, topic_url, request_otp)
    if not cap.get("ok"):
        return {"ok": False, "message": cap.get("message", "session capture failed"),
                "applied": [], "failed": [], "_fallback": True}

    token = cap["token"]
    applied, failed, unreached = [], [], []
    async with httpx.AsyncClient(timeout=60) as c:
        # 1) tags -> name -> id
        tr = await c.get(BFF_TAGS_URL, headers={**_API_BASE_HEADERS, "authorization": token})
        tr.raise_for_status()
        name_to_id = {t["name"]: t["id"] for t in tr.json() if t.get("name")}
        log.info("apply-api: %d tags available", len(name_to_id))

        # 2) replay each captured msearch batch VERBATIM and merge the hits.
        # The app's own batch already returns the individual member documents
        # (similar sub-posts) — rewriting the query broke that, so we send it
        # unchanged. url -> {documentId, matchSentence, keywords}.
        docmap = {}
        for i, ms in enumerate(cap["msearches"]):
            try:
                mr = await c.post(
                    ms["url"],
                    headers={**_API_BASE_HEADERS, "authorization": f"Bearer {token}",
                             "content-type": "application/json",
                             "x-credit-pool-id": "mi-explore-brand-volume-ip",
                             "x-product-type": "explore-dataservice"},
                    content=ms["body"],
                )
                if mr.status_code == 200:
                    found = _hits_from_msearch(mr.text)
                    for k, v in found.items():
                        docmap.setdefault(k, v)
                    log.info("apply-api: msearch batch %d/%d -> %d docs (running total %d)",
                              i + 1, len(cap["msearches"]), len(found), len(docmap))
                else:
                    log.warning("apply-api: msearch batch %d returned status %s", i + 1, mr.status_code)
            except Exception as e:
                log.warning("apply-api: msearch batch %d errored: %s: %s", i + 1, type(e).__name__, e)

        log.info("apply-api: %d documents resolved; %d targets to tag", len(docmap), len(to_apply))
        if not docmap:
            return {"ok": False, "message": "msearch returned no documents",
                    "applied": [], "failed": [], "unreached": [], "_fallback": True}

        # 3) enqueue a tag per target — EXACT match only.
        # No post-id fallback: matching by post-id alone tagged the parent
        # (oxb9six) for a target comment (oxfei07). The canonical key includes the
        # comment id, so an exact match is the ONLY safe rule. If the exact
        # document isn't present, we leave it unreached rather than risk tagging
        # the wrong mention. (hiddenDocuments=included above ensures the exact
        # sub-post is actually in the results, so exact match resolves it.)
        for key, val in to_apply.items():
            hit = docmap.get(key)
            if not hit:
                unreached.append(val["orig"])
                continue
            tag_name = normalize_tag(val["tag"])
            tag_id = name_to_id.get(tag_name) or name_to_id.get(val["tag"])
            if not tag_id:
                log.warning("apply-api: tag %r not found in account tag list", tag_name)
                failed.append({"permalink": val["orig"], "tag": val["tag"]})
                continue
            body = json.dumps({
                "documents": [{"documentId": hit["documentId"],
                               "matchSentence": hit["matchSentence"],
                               "keywords": hit["keywords"]}],
                "tagIds": [tag_id],
            })
            er = await c.post(BFF_ENQUEUE_URL,
                              headers={**_API_BASE_HEADERS, "authorization": token,
                                       "content-type": "application/json"},
                              content=body)
            if er.status_code in (200, 202):
                log.info("apply-api: tagged %s -> %s", val["orig"], tag_name)
                applied.append({"permalink": val["orig"], "tag": tag_name})
            else:
                log.warning("apply-api: enqueue failed for %s (status=%s)", val["orig"], er.status_code)
                failed.append({"permalink": val["orig"], "tag": val["tag"]})

    log.info("apply-api: done applied=%d failed=%d unreached=%d",
              len(applied), len(failed), len(unreached))
    return {"ok": True, "message": f"Applied {len(applied)} tag(s) via API.",
            "applied": applied, "skipped_already": [], "failed": failed, "unreached": unreached}


async def switch_meltwater_account(page, account_hint: str) -> bool:
    """Some logins (e.g. an analyst whose SSO drops them on a personal 'Buddy'
    workspace) start in the WRONG Meltwater account, so the brand's saved search
    404s and the feed is empty. This switches into the workspace that owns the
    brand: click the top-right account menu -> Account -> type the brand in
    'Find account' -> pick the first matching account. `account_hint` is what to
    type (the run brand name). Returns True if a switch was attempted+selected."""
    log.info("apply: ACCOUNT SWITCH — need to switch into the '%s' workspace", account_hint)
    try:
        # 1) open the top-right account menu. The person icon lives inside the
        #    account button; click it (or its clickable ancestor).
        opened = False
        for sel in ('[data-testid="PersonIcon"]',
                    'button:has([data-testid="PersonIcon"])',
                    '[data-testid="PersonIcon"] >> xpath=ancestor::button[1]'):
            try:
                el = await page.query_selector(sel)
            except Exception:
                el = None
            if el:
                try:
                    await el.click()
                    opened = True
                    log.info("apply: ACCOUNT SWITCH — clicked account button via %s", sel)
                    break
                except Exception:
                    continue
        if not opened:
            log.warning("apply: ACCOUNT SWITCH — account button not found %s", await _diag(page))
            return False
        # confirm the menu actually opened (Logout / the email should be visible)
        try:
            await page.wait_for_selector('text="Logout"', timeout=6000)
            log.info("apply: ACCOUNT SWITCH — account menu is open")
        except Exception:
            log.warning("apply: ACCOUNT SWITCH — menu didn't open (no 'Logout' seen) %s", await _diag(page))

        # 2) click "Account" in that menu. Prefer a menuitem role so we never hit
        #    the left-nav "Account"; fall back to the LAST plain "Account" (the
        #    overlay renders after the nav in the DOM). Verify by waiting for the
        #    "Find account" box; if it doesn't appear, try the next candidate.
        found_search = False
        candidates = []
        try:
            mi = page.get_by_role("menuitem", name=re.compile(r"account", re.I))
            for i in range(await mi.count()):
                candidates.append(mi.nth(i))
        except Exception:
            pass
        try:
            tx = page.get_by_text("Account", exact=True)
            cnt = await tx.count()
            # iterate from last to first (overlay item is usually last)
            for i in range(cnt - 1, -1, -1):
                candidates.append(tx.nth(i))
        except Exception:
            pass
        for cand in candidates:
            try:
                await cand.click(timeout=3000)
            except Exception:
                continue
            try:
                await page.wait_for_selector(
                    'xpath=//*[contains(translate(normalize-space(.),"FIND ACCOUNT","find account"),"find account")]',
                    timeout=3500)
                found_search = True
                log.info("apply: ACCOUNT SWITCH — opened the 'Accounts' panel")
                break
            except Exception:
                continue
        if not found_search:
            log.warning("apply: ACCOUNT SWITCH — could not open the 'Accounts' panel (no 'Find account') %s",
                         await _diag(page))
            return False
        await page.wait_for_timeout(400)

        # 3) type the brand into the "Find account" box (the input right after the
        #    'Find account' label).
        find = await page.query_selector(
            'xpath=//*[contains(translate(text(),"FIND ACCOUNT","find account"),"find account")]/following::input[1]')
        if not find:
            find = await page.query_selector('input[type="text"]:not([disabled])')
        if not find:
            log.warning("apply: ACCOUNT SWITCH — 'Find account' input not found %s", await _diag(page))
            return False
        await find.click()
        await find.fill(account_hint)
        log.info("apply: ACCOUNT SWITCH — typed %r into 'Find account'", account_hint)
        await page.wait_for_timeout(1500)

        # 4) click the first matching account result (a clickable row that appears
        #    below the search box, NOT the input itself).
        rowloc = page.get_by_text(re.compile(re.escape(account_hint), re.I))
        n = await rowloc.count()
        if n == 0:
            log.warning("apply: ACCOUNT SWITCH — no account matched %r %s", account_hint, await _diag(page))
            return False
        await rowloc.last.click()
        log.info("apply: ACCOUNT SWITCH — clicked the account matching %r (of %d matches)", account_hint, n)

        # switching navigates via /switchingCompany/... then reloads home.
        try:
            await page.wait_for_load_state("networkidle", timeout=25000)
        except Exception:
            pass
        await page.wait_for_timeout(2000)

        # 5) verify we actually switched — the top-right account button should now
        #    read the target environment (e.g. "Kaseya - Fairhair"), not the
        #    personal "…- Buddy" workspace. Poll a few times, since the switch
        #    triggers a /switchingCompany reload.
        want = account_hint.strip().lower()
        label = ""
        for _ in range(6):
            label = (await _current_account_label(page)) or ""
            if want in label.lower() and "buddy" not in label.lower():
                log.info("apply: ACCOUNT SWITCH — VERIFIED switched into %r "
                          "(account button now reads %r) ✓", account_hint, label)
                return True
            await page.wait_for_timeout(1500)
        # Fallback to a page-text check if the button label couldn't be read.
        diag = await _diag(page)
        if want in diag.lower() and "buddy" not in diag.lower():
            log.info("apply: ACCOUNT SWITCH — VERIFIED switched into %r (via page text) ✓ %s",
                      account_hint, diag)
            return True
        log.warning("apply: ACCOUNT SWITCH — could NOT verify switch into %r "
                     "(account button=%r) %s", account_hint, label, diag)
        return False
    except Exception as e:
        log.warning("apply: ACCOUNT SWITCH — failed: %s: %s", type(e).__name__, e)
        return False


async def _current_account_label(page) -> str:
    """Read the top-right account button's visible text (e.g. 'Kaseya - Fairhair'
    or 'Ritu Sharma - Buddy'), used to confirm an account switch actually took."""
    for sel in (
        'button:has([data-testid="PersonIcon"])',
        '[data-testid="PersonIcon"] >> xpath=ancestor::button[1]',
        '[data-testid="PersonIcon"] >> xpath=ancestor::*[self::button or @role="button"][1]',
    ):
        try:
            el = await page.query_selector(sel)
            if el:
                t = (await el.inner_text()).strip()
                if t:
                    return " ".join(t.split())
        except Exception:
            continue
    return ""


async def open_brand_search_tile(page, brand_hint: str) -> bool:
    """After switching into the brand's workspace we land on Home, which shows a
    'Pick up where you left off' row of saved-search tiles (e.g. 'Kaseya V2 |
    Reddit'). Clicking the tile does the in-app navigation that actually renders
    the results — more reliable in automation than re-opening the saved URL.
    Prefer a tile whose title contains the brand; else click the first tile."""
    log.info("apply: OPEN SEARCH — looking for a 'Pick up where you left off' tile for %r", brand_hint)
    try:
        try:
            await page.wait_for_selector('text="Pick up where you left off"', timeout=10000)
        except Exception:
            log.info("apply: OPEN SEARCH — 'Pick up where you left off' heading not seen %s", await _diag(page))
        await page.wait_for_timeout(600)

        clicked = False
        clicked_label = ""
        # 1) prefer a tile whose title contains the brand name.
        try:
            loc = page.get_by_text(re.compile(re.escape(brand_hint), re.I))
            for i in range(await loc.count()):
                el = loc.nth(i)
                try:
                    txt = (await el.inner_text()).strip()
                except Exception:
                    continue
                if "|" in txt or "reddit" in txt.lower() or "news" in txt.lower() or "social" in txt.lower():
                    await el.click()
                    clicked, clicked_label = True, txt.replace("\n", " ")[:50]
                    break
        except Exception:
            pass
        # 2) fallback: the first search-like tile ("<name> | <source>").
        if not clicked:
            try:
                first = page.get_by_text(re.compile(r"\|\s*(Reddit|News|Social|Twitter|X|Web|Blogs)\b", re.I)).first
                if await first.count() > 0:
                    clicked_label = (await first.inner_text()).strip().replace("\n", " ")[:50]
                    await first.click()
                    clicked = True
            except Exception:
                pass
        if not clicked:
            log.info("apply: OPEN SEARCH — no tile matched; will fall back to the saved URL")
            return False

        log.info("apply: OPEN SEARCH — clicked tile %r; waiting for results", clicked_label)
        try:
            await page.wait_for_url("**/explore/results**", timeout=20000)
        except Exception:
            pass
        await page.wait_for_timeout(1500)
        if "explore/results" not in page.url.lower():
            log.info("apply: OPEN SEARCH — did not reach results page %s", await _diag(page))
            return False
        log.info("apply: OPEN SEARCH — results page open, now on %s", page.url)
        return True
    except Exception as e:
        log.warning("apply: OPEN SEARCH — failed: %s: %s", type(e).__name__, e)
        return False


async def _explore_editor_ready(page, timeout: int = 3000) -> bool:
    """True once the Advanced-search EDITOR is on screen (the screen that hosts
    the 'Advanced search ▾' dropdown). Detected by editor-only landmarks —
    'Refine with AI', 'Supported operators', or being on the /explore/results
    (editor) URL — so the Explore landing page (which only shows the 'Advanced
    search' card) never counts as ready.

    Polls with get_by_text rather than a comma-joined `text=` selector: Playwright
    only OR-joins CSS selectors with commas, not text-engine selectors, so the
    joined form silently matches nothing (this was the 25s-timeout bug)."""
    landmarks = (
        re.compile(r"Refine with AI", re.I),
        re.compile(r"Supported operators", re.I),
        re.compile(r"update results", re.I),
    )
    waited = 0
    step = 1000
    while waited <= timeout:
        # Require an actual editor LANDMARK to be painted — NOT just the
        # /explore/results URL, which flips before the toolbar renders (racing it
        # made us try to open the dropdown against a blank shell).
        for rx in landmarks:
            try:
                if await page.get_by_text(rx).count() > 0:
                    return True
            except Exception:
                pass
        # heartbeat so the logs show we're still waiting (not stuck)
        if waited and waited % 5000 == 0:
            log.info("apply: ADV SEARCH (SSO) — still waiting for the search editor "
                      "(%ds elapsed, url=%s)", waited // 1000, page.url)
        await page.wait_for_timeout(step)
        waited += step
    return False


async def open_advanced_search_via_explore(page, brand_name: str) -> bool:
    """SSO pipeline — after switching into the brand's environment/account, reach
    the brand's Reddit Advanced search results feed by driving the Explore UI:

      1. click "Explore" (left nav)
      2. click the "Advanced search" card
      3. open the "Advanced search ▾" dropdown (top toolbar)
      4. type the BRAND NAME (e.g. "Kaseya V2") into the dropdown's "Find" box
      5. among the matches, click the one whose name contains "Reddit"

    Returns True once the results feed is showing. This replaces the Home-tile
    approach for @meltwater.com accounts (open_brand_search_tile), which was less
    reliable. `brand_name` is the Sentiment Tagger run brand (matches the search title, e.g. "Kaseya V2 | Reddit")."""
    log.info("apply: ADV SEARCH (SSO) — reaching the Reddit search for %r via Explore", brand_name)
    try:
        # 1) Explore (left nav).
        clicked_explore = False
        for getter in ("link", "button"):
            try:
                loc = page.get_by_role(getter, name=re.compile(r"^\s*Explore\s*$", re.I))
                if await loc.count() > 0:
                    await loc.first.click()
                    clicked_explore = True
                    break
            except Exception:
                pass
        if not clicked_explore:
            try:
                el = await page.query_selector('a:has-text("Explore"), [data-testid*="explore" i]')
                if el:
                    await el.click()
                    clicked_explore = True
            except Exception:
                pass
        if not clicked_explore:
            log.warning("apply: ADV SEARCH (SSO) — could not find the Explore nav item %s", await _diag(page))
            return False
        log.info("apply: ADV SEARCH (SSO) — clicked Explore")
        # The Explore SPA (e.g. /a/explore/list) renders asynchronously — the
        # page is briefly blank (bodyText=''). Wait for it to actually paint
        # before looking for the card, or we race a blank DOM (the bug seen live).
        # Generous timeouts + heartbeat logs: let it take as long as it needs.
        try:
            await page.wait_for_load_state("networkidle", timeout=60000)
        except Exception:
            pass

        # 2) "Advanced search" card — this opens the search editor (which is what
        #    hosts the Advanced search dropdown). Wait for it to render, click it, and
        #    confirm the editor loaded before continuing. Skip only if the editor
        #    is somehow already open.
        editor_ready = await _explore_editor_ready(page)
        if not editor_ready:
            log.info("apply: ADV SEARCH (SSO) — waiting for the 'Advanced search' card to render "
                      "(up to 90s)…")
            card = None
            try:
                await page.wait_for_selector('text=/^\\s*Advanced search\\s*$/i', timeout=90000)
                card = page.get_by_text(re.compile(r"^\s*Advanced search\s*$", re.I))
            except Exception:
                card = None
            if not card or await card.count() == 0:
                log.warning("apply: ADV SEARCH (SSO) — 'Advanced search' card never rendered %s",
                             await _diag(page))
                return False
            await card.first.click()
            log.info("apply: ADV SEARCH (SSO) — clicked the 'Advanced search' card; waiting for the "
                      "editor to load (up to 120s, heartbeat every 5s)")
            editor_ready = await _explore_editor_ready(page, timeout=120000)
            if not editor_ready:
                log.warning("apply: ADV SEARCH (SSO) — search editor did not load after the card %s",
                             await _diag(page))
                return False
        log.info("apply: ADV SEARCH (SSO) — search editor is ready")

        # 3) open the "Advanced search ▾" dropdown in the top toolbar.
        #    The toolbar toggle can render a beat after the editor landmarks, so
        #    POLL for it (up to 60s, heartbeat every 5s) rather than trying once.
        #    IMPORTANT: the editor page ALSO has a global "Find" box in the top nav
        #    with the same placeholder, so we must NOT confirm the dropdown by a
        #    "Find" input (that box is always present). Confirm instead by the
        #    dropdown's own menu contents ("New search" / "ALL SEARCHES").
        toggle_sels = (
            'button:has-text("Advanced search")',
            '[role="button"]:has-text("Advanced search")',
            'text=/^\\s*Advanced search\\s*$/i',
        )
        opened_dd = False
        waited = 0
        while waited <= 60000 and not opened_dd:
            for cand in toggle_sels:
                try:
                    el = await page.query_selector(cand)
                    if not el or not await el.is_visible():
                        continue
                    await el.click()
                    # confirm the dropdown MENU opened via its own contents (poll
                    # with get_by_text; a comma text= selector matches nothing)
                    for _ in range(6):
                        for rx in (re.compile(r"New search", re.I),
                                   re.compile(r"ALL SEARCHES", re.I)):
                            try:
                                if await page.get_by_text(rx).count() > 0:
                                    opened_dd = True
                                    break
                            except Exception:
                                pass
                        if opened_dd:
                            break
                        await page.wait_for_timeout(1000)
                    if opened_dd:
                        break
                except Exception:
                    continue
            if opened_dd:
                break
            if waited and waited % 5000 == 0:
                log.info("apply: ADV SEARCH (SSO) — still waiting for the 'Advanced search' toolbar "
                          "toggle to render/open (%ds elapsed, url=%s)", waited // 1000, page.url)
            await page.wait_for_timeout(1000)
            waited += 1000
        if not opened_dd:
            log.warning("apply: ADV SEARCH (SSO) — could not open the Advanced search dropdown %s", await _diag(page))
            return False
        log.info("apply: ADV SEARCH (SSO) — opened the Advanced search dropdown")
        await page.wait_for_timeout(800)

        # 4) type the brand name into the DROPDOWN's "Find" box — NOT the global
        #    nav "Find". The dropdown popover renders after the nav in the DOM, so
        #    among the visible "Find" inputs its own is the LAST one. Prefer a Find
        #    input that sits above the dropdown's "New search" row when we can find
        #    it; otherwise use the last visible Find input.
        find = None
        try:
            find = await page.query_selector(
                'xpath=(//*[contains(translate(normalize-space(.),'
                '"NEW SEARCH","new search"),"new search")]/preceding::input[@placeholder][1])[last()]')
            if find and not await find.is_visible():
                find = None
        except Exception:
            find = None
        if not find:
            try:
                inputs = await page.query_selector_all('input[placeholder*="Find" i]')
                for el in reversed(inputs):  # dropdown's Find is the last-rendered
                    if await el.is_visible():
                        find = el
                        break
            except Exception:
                find = None
        if not find:
            log.warning("apply: ADV SEARCH (SSO) — dropdown 'Find' box not found %s", await _diag(page))
            return False
        await find.click()
        await find.fill(brand_name)
        log.info("apply: ADV SEARCH (SSO) — typed %r into the Advanced search 'Find' box; "
                  "waiting for matches to load (up to 30s, heartbeat every 5s)", brand_name)

        # 5) among the matches, click the search whose name contains "Reddit".
        #    Match on both the brand name and "Reddit" so we never pick another
        #    brand's Reddit search that happens to render. Give the filtered list
        #    time to render rather than racing it.
        clicked_label = ""
        reddit_re = re.compile(
            re.escape(brand_name) + r".*reddit|reddit.*" + re.escape(brand_name), re.I)
        waited = 0
        while waited <= 30000:
            try:
                if await page.get_by_text(reddit_re).count() > 0:
                    break
            except Exception:
                pass
            if waited and waited % 5000 == 0:
                log.info("apply: ADV SEARCH (SSO) — still waiting for the %r Reddit search to "
                          "appear (%ds elapsed)", brand_name, waited // 1000)
            await page.wait_for_timeout(1000)
            waited += 1000
        try:
            loc = page.get_by_text(reddit_re)
            if await loc.count() > 0:
                clicked_label = (await loc.first.inner_text()).strip().replace("\n", " ")[:60]
                await loc.first.click()
                log.info("apply: ADV SEARCH (SSO) — clicked search %r", clicked_label)
        except Exception:
            pass
        if not clicked_label:
            # Fallback: any visible row containing "Reddit" (the Find box already
            # filtered to this brand's searches).
            try:
                loc = page.get_by_text(re.compile(r"reddit", re.I))
                if await loc.count() > 0:
                    clicked_label = (await loc.first.inner_text()).strip().replace("\n", " ")[:60]
                    await loc.first.click()
                    log.info("apply: ADV SEARCH (SSO) — clicked Reddit search (fallback) %r", clicked_label)
            except Exception:
                pass
        if not clicked_label:
            log.warning("apply: ADV SEARCH (SSO) — no 'Reddit' search matched %r %s",
                         brand_name, await _diag(page))
            return False

        # results should load in place (the editor stays on /explore).
        try:
            await page.wait_for_load_state("networkidle", timeout=25000)
        except Exception:
            pass
        await page.wait_for_timeout(1500)
        log.info("apply: ADV SEARCH (SSO) — Reddit search open, now on %s", page.url)
        return True
    except Exception as e:
        log.warning("apply: ADV SEARCH (SSO) — failed: %s: %s", type(e).__name__, e)
        return False


async def _is_logged_in(page) -> bool:
    """True if opening app.meltwater.com lands on the authenticated app (the
    account button is present) rather than a Meltwater/Microsoft login page. Used
    to decide whether a reused saved session is still valid — if so we skip the
    whole login/OTP flow entirely."""
    try:
        await page.goto(config.MELTWATER_URL, wait_until="domcontentloaded", timeout=30000)
    except Exception:
        return False
    for _ in range(20):
        url = (page.url or "").lower()
        if ("authorize.meltwater.com" in url or "microsoftonline.com" in url
                or "/login" in url):
            return False
        try:
            el = await page.query_selector(
                '[data-testid="PersonIcon"], button:has([data-testid="PersonIcon"])')
            if el and await el.is_visible():
                return True
        except Exception:
            pass
        await page.wait_for_timeout(1000)
    return False


async def apply_results_to_meltwater(email: str, password: str, topic_url: str, results: list[dict],
                                     request_otp=None, account_hint=None, brand_name=None,
                                     saved_state=None, on_state_captured=None) -> dict:
    """
    Login-automation path: fills the Auth0 email/password/passkey flow (or, for
    @meltwater.com accounts, the Microsoft Entra SSO + SMS-OTP flow), then tags
    posts. results: classification results, each with permalink/tag/action.
    Only entries with action == 'apply' and a non-empty tag are actually applied.

    Session reuse (SSO): if `saved_state` (a Playwright storage_state JSON string)
    is given, it's loaded into the browser first. If that session is still valid,
    login/OTP is skipped entirely. If it's expired, we DO NOT auto-prompt OTP —
    we return `_session_expired` so the caller can ask the analyst to clear it
    (per the product rule: never OTP while a saved session exists). On a fresh
    login (no saved_state), the resulting session is captured via
    `on_state_captured(state_json)` so subsequent runs need no OTP.
    """
    to_apply = _build_to_apply(results)
    log.info("apply_results_to_meltwater: %d posts to tag, topic_url=%s (saved_session=%s)",
              len(to_apply), topic_url, bool(saved_state))
    # SSO (@meltwater.com) runs — account_hint is the environment — reach the feed
    # via account switch + Advanced search, so a topic URL isn't required.
    bad_input = _check_apply_inputs(to_apply, topic_url, require_topic=not bool(account_hint))
    if bad_input:
        return bad_input

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=CHROMIUM_LAUNCH_ARGS)
        # Load the saved browser session (cookies + localStorage) if we have one.
        ctx_kwargs = {}
        if saved_state:
            try:
                ctx_kwargs["storage_state"] = json.loads(saved_state)
            except Exception as e:
                log.warning("apply: saved session couldn't be parsed (%s) — ignoring it", e)
        context = await _new_browser_context(browser, **ctx_kwargs)
        page = await context.new_page()

        if saved_state:
            log.info("apply: SESSION REUSE — trying the saved Meltwater session (no OTP)…")
            if await _is_logged_in(page):
                log.info("apply: SESSION REUSE — saved session is VALID; skipping login/OTP ✓")
            else:
                # Expired/invalid. Per the product rule, do NOT auto-prompt OTP
                # while a saved session exists — ask the user to clear it.
                log.warning("apply: SESSION REUSE — saved session is EXPIRED/invalid; NOT prompting "
                             "OTP. User must clear it on Profile to log in fresh.")
                await browser.close()
                return {"ok": False, "applied": [], "failed": [], "_session_expired": True,
                        "message": ("Your saved Meltwater session has expired. Go to Profile → "
                                     "'Log out of Meltwater / clear saved session', then run Apply "
                                     "again to log in once (you'll enter an SMS code that one time).")}
        else:
            ok, msg = await login_to_meltwater(page, email, password, request_otp)
            if not ok:
                log.error("apply_results_to_meltwater: login failed — %s", msg)
                await browser.close()
                return {"ok": False, "message": msg, "applied": [], "failed": []}
            # Capture the fresh logged-in session so future runs skip OTP.
            if on_state_captured is not None:
                try:
                    state = await context.storage_state()
                    on_state_captured(json.dumps(state))
                    log.info("apply: SESSION SAVE — captured the logged-in session for reuse "
                              "(future runs won't need OTP until it's cleared) ✓")
                except Exception as e:
                    log.warning("apply: SESSION SAVE — could not capture the session: %s", e)

        # Post-login STEP 0 (SSO / @meltwater.com only) — switch into the brand's
        # ENVIRONMENT account, then reach the brand's Reddit search through
        # the Explore UI (Explore -> Advanced search -> Advanced search dropdown ->
        # find brand name -> pick the Reddit search). `account_hint` here is the
        # brand's ENVIRONMENT (e.g. "Kaseya - Fairhair"); `brand_name` is the run
        # brand (e.g. "Kaseya V2"), which matches the Advanced search title.
        feed_opened = False
        if account_hint:
            switched = await switch_meltwater_account(page, account_hint)
            if not switched:
                log.error("apply: POST-LOGIN STEP FAILED — could not switch into the "
                           "'%s' environment; aborting so we never tag in the wrong account",
                           account_hint)
                await browser.close()
                return {"ok": False, "applied": [], "failed": [],
                        "message": (f"Could not switch into the '{account_hint}' Meltwater "
                                     "account/environment. Check the brand's Environment value in "
                                     "Brand Studio matches an account name in Meltwater exactly.")}
            feed_opened = await open_advanced_search_via_explore(page, brand_name or account_hint)
            if not feed_opened:
                log.error("apply: POST-LOGIN STEP FAILED — switched into %r but could not open "
                           "the Reddit search for %r", account_hint, brand_name)
                await browser.close()
                return {"ok": False, "applied": [], "failed": [],
                        "message": (f"Switched into '{account_hint}', but couldn't open the "
                                     f"Reddit search for '{brand_name}'. Confirm a "
                                     f"search named like '{brand_name} | Reddit' exists in that account.")}

        # Fallback (and the normal path for non-SSO accounts) — open the brand's
        # saved topic URL directly. This is the analyst's "My Meltwater topic URL
        # (personal override)" (falling back to the org default), resolved
        # server-side. We're already logged in, so the feed shows directly.
        if not feed_opened:
            log.info("apply: POST-LOGIN STEP — opening the brand's saved topic URL: %s", topic_url)
            try:
                await page.goto(topic_url, wait_until="domcontentloaded", timeout=30000)
            except Exception as e:
                log.error("apply: POST-LOGIN STEP FAILED — could not open the topic URL: %s: %s",
                           type(e).__name__, e)
                await browser.close()
                return {"ok": False, "message": f"Could not open the Meltwater topic feed: {e}", "applied": [], "failed": []}
            await asyncio.sleep(2)
            if "login" in page.url.lower():
                log.error("apply: POST-LOGIN STEP — bounced back to a login page (session not carried) %s",
                           await _diag(page))
            else:
                log.info("apply: POST-LOGIN STEP — topic feed loaded, now on %s", page.url)

        report = await _walk_feed_and_tag(page, to_apply)
        await browser.close()
    return report


async def apply_via_session(storage_value: str, topic_url: str, results: list[dict]) -> dict:
    """
    Session-injection path (preferred): sets Meltwater's cached Auth0 token
    directly in Local Storage before the app's own scripts run, so the SPA
    considers itself already logged in — no email/password/passkey screens.
    Requires a value the user copied from their own browser's
    Application -> Local Storage -> app.meltwater.com -> AUTH0_STORAGE_KEY.
    """
    to_apply = _build_to_apply(results)
    log.info("apply_via_session: %d posts to tag, topic_url=%s", len(to_apply), topic_url)
    bad_input = _check_apply_inputs(to_apply, topic_url)
    if bad_input:
        return bad_input

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=CHROMIUM_LAUNCH_ARGS)
        context = await _new_browser_context(browser)

        # Inject the cached token into Local Storage BEFORE any page script
        # runs, on every document load in this context -- this is what lets
        # Auth0's SDK see a valid cached session on first paint.
        init_script = (
            "try { window.localStorage.setItem(%s, %s); } catch (e) {}"
            % (_js_string(AUTH0_STORAGE_KEY), _js_string(storage_value))
        )
        await context.add_init_script(init_script)
        # Also set the lightweight "is authenticated" flags some Auth0 SPA
        # versions check before deciding whether to hit the network at all.
        await context.add_cookies([
            {"name": "auth0.is.authenticated", "value": "true", "domain": "app.meltwater.com", "path": "/"},
        ])

        page = await context.new_page()
        log.info("apply_via_session: navigating to topic feed with injected session")
        try:
            await page.goto(topic_url, wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            log.error("apply_via_session: failed to open topic_url: %s: %s", type(e).__name__, e)
            await browser.close()
            return {"ok": False, "message": f"Could not open the Meltwater topic feed: {e}", "applied": [], "failed": []}

        try:
            await page.wait_for_load_state("networkidle", timeout=20000)
        except Exception:
            log.debug("apply_via_session: networkidle wait timed out (continuing anyway)")

        if "login" in page.url.lower():
            log.error("apply_via_session: redirected to login — session expired or invalid %s", await _diag(page))
            await browser.close()
            return {"ok": False,
                    "message": "Your saved Meltwater session has expired or is invalid. Go to Profile and "
                               "paste a fresh value from your browser's Local Storage.",
                    "applied": [], "failed": []}

        log.info("apply_via_session: session accepted, feed loaded %s", await _diag(page))
        await asyncio.sleep(2)
        report = await _walk_feed_and_tag(page, to_apply)
        await browser.close()
    return report


def _js_string(s: str) -> str:
    """JSON-encode a Python string for safe embedding in an injected JS snippet."""
    import json
    return json.dumps(s)
