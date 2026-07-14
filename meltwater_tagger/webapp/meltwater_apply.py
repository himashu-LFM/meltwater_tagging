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

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_THIS_DIR))  # project root (apply_tags, config)
sys.path.insert(0, _THIS_DIR)  # webapp/ itself (logging_setup), for standalone import
from playwright.async_api import async_playwright

from apply_tags import (
    SELECTORS, norm_permalink, get_card_permalink, card_existing_tag,
    expand_similar_if_needed, apply_tag_to_card,
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


# Result-card container selector. Broadened beyond the original guess; the
# real one is confirmed via the feed diagnostic below and can be pinned via env.
# Confirmed live: Meltwater's feed is a Virtuoso virtualized list; each result
# card is a direct child of [data-testid="virtuoso-item-list"].
CARD_SELECTOR = os.environ.get(
    "MELTWATER_CARD_SELECTOR",
    '[data-testid="virtuoso-item-list"] > div, [data-index], [data-item-index]',
)


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
            return
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


async def login_to_meltwater(page, email: str, password: str) -> tuple[bool, str]:
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
            return { before, after: s.scrollTop, tag: s.tagName, testid: s.getAttribute && s.getAttribute('data-testid') };
        }""")
        log.debug("apply: scrolled feed %s", moved)
    except Exception as e:
        log.debug("apply: scroll failed (%s) — falling back to mouse wheel", e)
        try:
            await page.mouse.wheel(0, 3000)
        except Exception:
            pass


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
      climbing from each post link to the smallest ancestor that owns its own
      "Open article in new tab" control. This yields the parent's own card AND
      each similar sub-post's own card as separate elements, so the exact
      sub-post gets hovered and tagged instead of its parent."""
    top = list(await page.query_selector_all(CARD_SELECTOR))
    if not include_nested:
        return top

    try:
        arr = await page.evaluate_handle(
            """(OPEN) => {
                const seen = new Set();
                const cards = [];
                document.querySelectorAll('a[href]').forEach(a => {
                    const href = a.getAttribute('href') || '';
                    if (!href || href.indexOf('meltwater') !== -1) return;
                    if (!/reddit\\.com|^https?:/i.test(href)) return;
                    // 1) smallest ancestor that owns an "Open article" control
                    let base = a;
                    while (base && base !== document.body) {
                        if (base.querySelector && base.querySelector(OPEN)) break;
                        base = base.parentElement;
                    }
                    if (!base || base === document.body) return;
                    // 2) climb UP while the element still wraps exactly ONE
                    //    mention (one Open-article control). The largest such
                    //    ancestor is the full mention card — body PLUS its hover
                    //    toolbar (where the Tag icon lives) — but never the group
                    //    container, which would hold 2+ Open-article controls.
                    let card = base, p = base.parentElement;
                    while (p && p !== document.body &&
                           p.querySelectorAll(OPEN).length === 1) {
                        card = p; p = p.parentElement;
                    }
                    if (!seen.has(card)) { seen.add(card); cards.push(card); }
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
            log.debug("apply[expand]: derived %d per-mention card(s) from post links", len(cards))
            return cards
    except Exception as e:
        log.debug("apply[expand]: per-mention derivation failed (%s) — falling back to list items", e)
    return top


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
    rounds = 0
    MAX_ROUNDS = 120
    label = "expand" if expand_similar else ("fallback" if use_fallback else "exact")
    while no_new_rounds < 4 and rounds < MAX_ROUNDS and len(handled) < len(to_apply):
        rounds += 1
        before_seen = len(seen_any)

        if expand_similar:
            # The collapsed-state selector never matches an already-open group,
            # so clicking every match can only OPEN groups, never re-collapse one.
            toggles = await page.query_selector_all(SELECTORS["similar_collapsed"])
            if toggles:
                log.info("apply[expand]: opening %d Similar group(s) in view", len(toggles))
            for t in toggles:
                try:
                    await t.click()
                    await asyncio.sleep(0.4)
                except Exception:
                    pass

        cards = await _candidate_cards(page, include_nested=expand_similar)
        for card in cards:
            # Note Similar-group parents (before they're expanded) so the
            # post-id fallback can never tag the parent of a grouped post.
            try:
                has_similar = await card.query_selector(SELECTORS["similar_collapsed"])
            except Exception:
                has_similar = None
            try:
                permalink = await get_card_permalink(card)
            except Exception:
                continue  # detached mid-read; ignore, it'll re-render
            if not permalink:
                continue
            seen_any.add(permalink)
            if has_similar:
                pid = reddit_post_id(permalink)
                if pid:
                    similar_pids.add(pid)

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

        await _scroll_feed(page)
        await asyncio.sleep(1.2)
        if len(seen_any) == before_seen:
            no_new_rounds += 1
        else:
            no_new_rounds = 0
        log.debug("apply[%s]: round %d — seen=%d handled=%d/%d no_new=%d",
                   label, rounds, len(seen_any), len(handled), len(to_apply), no_new_rounds)
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
    await _wait_for_feed_and_diagnose(page)

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
    log.info("apply: done — applied=%d failed=%d already=%d unreached=%d",
              len(applied), len(failed), len(skipped_already), len(unreached))
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


def _check_apply_inputs(to_apply: dict, topic_url: str) -> dict | None:
    if not to_apply:
        log.warning("apply: nothing to apply — no results had action='apply' with a tag")
        return {"ok": False, "message": "No posts with an 'apply' action to tag.", "applied": [], "failed": []}
    if not topic_url:
        log.error("apply: no topic_url given")
        return {"ok": False, "message": "This brand has no Meltwater topic URL configured yet (set it once in Brand settings).",
                "applied": [], "failed": []}
    return None


async def apply_results_to_meltwater(email: str, password: str, topic_url: str, results: list[dict]) -> dict:
    """
    Login-automation path: fills the Auth0 email/password/passkey flow, then
    tags posts. results: classification results, each with permalink/tag/action.
    Only entries with action == 'apply' and a non-empty tag are actually applied.
    """
    to_apply = _build_to_apply(results)
    log.info("apply_results_to_meltwater: %d posts to tag, topic_url=%s", len(to_apply), topic_url)
    bad_input = _check_apply_inputs(to_apply, topic_url)
    if bad_input:
        return bad_input

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()

        ok, msg = await login_to_meltwater(page, email, password)
        if not ok:
            log.error("apply_results_to_meltwater: login failed — %s", msg)
            await browser.close()
            return {"ok": False, "message": msg, "applied": [], "failed": []}

        log.info("apply_results_to_meltwater: navigating to topic feed")
        try:
            await page.goto(topic_url, wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            log.error("apply_results_to_meltwater: failed to open topic_url: %s: %s", type(e).__name__, e)
            await browser.close()
            return {"ok": False, "message": f"Could not open the Meltwater topic feed: {e}", "applied": [], "failed": []}
        await asyncio.sleep(2)

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
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context()

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
