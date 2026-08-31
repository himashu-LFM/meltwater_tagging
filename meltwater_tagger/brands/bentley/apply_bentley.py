"""
Bentley Phase-2 apply — plan and apply MANY taxonomy tags per item.

How this differs from Kaseya apply (one sentiment tag per Reddit post):
  * MANY tags per item, across several families.
  * ADDITIVE — apply only the tags an item is MISSING; never remove or re-apply
    an existing tag.
  * ALLOWLIST — only ever apply Bentley tags (taxonomy.applicable_labels()).
    The Meltwater account is shared: Kaseya/Ninja/Blake Lively/Eufy/Test tags
    live in the same modal and must never be touched.
  * NOT-IN-SCOPE — an out-of-scope item gets the single "Not in scope" tag.
  * REVIEW — items the classifier flagged (unreadable/blocked/uncertain) are
    reported, never auto-applied.

Two layers:
  1. PLANNING (pure, fully testable) — turn classifier decisions + the tags an
     item already carries into an exact per-item plan (to_add / to_skip /
     blocked). No browser, no side effects.
  2. EXECUTION — drive Meltwater's UI to apply the plan, reusing the proven
     apply_tags primitives (hover -> Tag icon -> modal -> check -> Apply). Runs
     as a DRY RUN by default; a live run must be explicitly requested.

CLI:
    # dry run (no browser): show exactly what would be applied
    python -m brands.bentley.apply_bentley decisions_bentley.json --dry-run
"""

import argparse
import json
import sys

from brands.bentley import taxonomy

NOT_IN_SCOPE = "Not in scope"


# ---------------------------------------------------------------------------
# PLANNING (pure)
# ---------------------------------------------------------------------------
def parse_existing_tags(document_tags) -> set[str]:
    """Meltwater's Document Tags come as a ';'-joined string. Return them as a
    set of individual tag strings (empty set if none)."""
    if isinstance(document_tags, (list, set, tuple)):
        return {str(t).strip() for t in document_tags if str(t).strip()}
    return {t.strip() for t in (document_tags or "").split(";") if t.strip()}


def _target_tags(item: dict) -> list[str]:
    """The tags this item SHOULD carry, per its classification."""
    scope = item.get("scope")
    if scope == "out":
        return [NOT_IN_SCOPE]
    if scope == "in":
        # de-dup, preserve order
        return list(dict.fromkeys(t for t in (item.get("tags") or []) if t))
    return []  # review / error -> nothing to apply


def plan_item(item: dict, allow: set[str] | None = None) -> dict:
    """Compute the apply plan for one classified item.

    Returns a dict:
      {url, document_id, scope, action, to_add, to_skip, blocked, existing, reason}
      action ∈ {'apply', 'nochange', 'review'}
        apply    — has tags to add (to_add non-empty)
        nochange — everything it should have is already present
        review   — classifier didn't produce an applyable decision (needs a human)
    """
    if allow is None:
        allow = taxonomy.applicable_labels() | {NOT_IN_SCOPE}

    url = item.get("url", "")
    doc_id = (item.get("document_id") or "").strip().strip('"')
    scope = item.get("scope")
    existing = parse_existing_tags(item.get("document_tags", ""))

    if scope not in ("in", "out"):
        return {"url": url, "document_id": doc_id, "scope": scope, "action": "review",
                "to_add": [], "to_skip": [], "blocked": [], "existing": sorted(existing),
                "reason": item.get("reason", "needs manual review"),
                "needs_review": item.get("needs_review") or []}

    targets = _target_tags(item)
    # ALLOWLIST: never apply a tag that isn't a real Bentley Meltwater tag.
    applyable = [t for t in targets if t in allow]
    blocked = [t for t in targets if t not in allow]
    # ADDITIVE: only add what's missing; report what's already there.
    to_add = [t for t in applyable if t not in existing]
    to_skip = [t for t in applyable if t in existing]

    action = "apply" if to_add else "nochange"
    return {"url": url, "document_id": doc_id, "scope": scope, "action": action,
            "to_add": to_add, "to_skip": to_skip, "blocked": blocked,
            "existing": sorted(existing), "reason": item.get("reason", "")}


def build_plan(decisions: list[dict]) -> list[dict]:
    """Plan for every decision. Shared allowlist computed once."""
    allow = taxonomy.applicable_labels() | {NOT_IN_SCOPE}
    return [plan_item(d, allow) for d in decisions]


def plan_summary(plans: list[dict]) -> dict:
    import collections
    by_action = collections.Counter(p["action"] for p in plans)
    tags_to_add = sum(len(p["to_add"]) for p in plans)
    blocked = sorted({t for p in plans for t in p["blocked"]})
    return {"items": len(plans), "by_action": dict(by_action),
            "total_tags_to_add": tags_to_add, "blocked_nonbentley": blocked}


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------
def load_decisions(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    # Accept either a bare list (batch output) or {"decisions": [...]}.
    if isinstance(data, dict):
        return data.get("decisions") or data.get("results") or []
    return data


def print_dry_run(plans: list[dict]) -> None:
    s = plan_summary(plans)
    print("=" * 78)
    print("BENTLEY APPLY — DRY RUN (no changes made)")
    print("=" * 78)
    print(f"items: {s['items']}  |  by action: {s['by_action']}  |  tags to add: {s['total_tags_to_add']}")
    if s["blocked_nonbentley"]:
        print(f"⚠ blocked (not a Bentley Meltwater tag, will NEVER be applied): {s['blocked_nonbentley']}")
    print("-" * 78)
    for p in plans:
        if p["action"] == "apply":
            print(f"APPLY   [{p['scope']}] {p['url'][:64]}")
            print(f"        + {p['to_add']}")
            if p["to_skip"]:
                print(f"        (already present, skipped: {p['to_skip']})")
            if p["blocked"]:
                print(f"        (blocked non-Bentley: {p['blocked']})")
        elif p["action"] == "nochange":
            print(f"NOCHANGE[{p['scope']}] {p['url'][:64]}  (all target tags already present)")
        else:
            print(f"REVIEW          {p['url'][:64]}  — {p.get('reason','')[:60]}")
    print("=" * 78)


# ---------------------------------------------------------------------------
# EXECUTION (browser) — drives the Meltwater UI, reusing the PROVEN apply_tags
# primitives (hover -> Tag icon -> "Tag content" modal -> check -> Apply). The
# Kaseya normalize_tag/_tag_candidates logic is a no-op on Bentley taxonomy
# strings, so apply_tag_to_card applies a Bentley tag correctly as-is.
#
# ⚠ Needs a logged-in Meltwater session to validate; run with --live --walk-only
# first (matches cards + reports, applies NOTHING) before a real --live apply.
# ---------------------------------------------------------------------------
async def card_existing_bentley_tags(card):
    """Tags currently ON this card, read from its visible 'Remove <tag>' chips.
    This is the LIVE additive-skip source: never re-apply a tag already present,
    and (via the allowlist upstream) never touch another brand's tag."""
    out = set()
    try:
        chips = await card.query_selector_all('[aria-label^="Remove "]')
    except Exception:
        return out
    for chip in chips:
        try:
            if not await chip.is_visible():
                continue
            label = await chip.get_attribute("aria-label")
            if label:
                out.add(label.replace("Remove ", "", 1).strip())
        except Exception:
            continue
    return out


async def apply_plan_to_card(page, card, to_add, dry_run, delay):
    """Apply each missing tag to one matched card. Reads the card's LIVE tags
    first and additively skips any already present. Returns (applied, skipped,
    failed) tag lists."""
    from apply_tags import apply_tag_to_card
    existing = await card_existing_bentley_tags(card)
    applied, skipped, failed = [], [], []
    for tag in to_add:
        if tag in existing:
            skipped.append(tag)
            continue
        ok = await apply_tag_to_card(page, card, tag, dry_run, delay)
        (applied if ok else failed).append(tag)
    return applied, skipped, failed


async def run_live(plans: list[dict], apply_changes: bool, capture_only: bool = False):
    """Walk the Meltwater feed and apply each plan's missing tags by matching the
    feed card's article URL to the plan's URL. Reuses apply_tags' feed + modal
    primitives and a persistent browser profile (log in manually on first run).

    apply_changes=False => WALK-ONLY: match cards and report what WOULD be
    applied, but change nothing (safe validation on the live feed).
    """
    import asyncio
    import config
    from playwright.async_api import async_playwright
    from apply_tags import norm_permalink, get_card_permalink

    # index plans by canonical URL; only items that actually need tags added
    want = {norm_permalink(p["url"]): p for p in plans if p["action"] == "apply" and p["to_add"]}
    if not want and not capture_only:
        print("Nothing to apply (no items with missing Bentley tags).")
        return
    delay = config.ACTION_DELAY_MS / 1000.0
    done, applied_all, skipped_all, failed_all = set(), [], [], []

    CARD_SEL = ('[data-testid="virtuoso-item-list"] > div, '
                '[data-testid="result-card"], article')

    async def _scroll(page):
        """Scroll Meltwater's virtualized (Virtuoso) list — mouse.wheel is a
        no-op on it, so scrollBy the actual scroller. Returns True if it moved."""
        try:
            return await page.evaluate("""() => {
                const s = document.querySelector('[data-testid="virtuoso-scroller"]')
                    || document.scrollingElement || document.documentElement;
                if (!s) return false;
                const before = s.scrollTop;
                s.scrollBy(0, Math.max(600, s.clientHeight * 0.85));
                return s.scrollTop !== before;
            }""")
        except Exception:
            try:
                await page.mouse.wheel(0, 3000)
            except Exception:
                pass
            return True

    import os
    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(config.USER_DATA_DIR, headless=config.HEADLESS)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        # Capture the real enqueue-document-tagging request(s) this run makes, so
        # the API apply path (api_apply.py --capture) can be finalized from a
        # genuine call rather than a guessed payload.
        cap_file = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "mw_capture.log"))

        async def _capture(req):
            # Capture ALL tag-related writes (any method) so we can find the call
            # that actually persists a manual tag — it may NOT be
            # enqueue-document-tagging. Log method + url + headers + body.
            try:
                u = req.url.lower()
                interesting = (
                    "enqueue-document-tagging" in u
                    or ("content-stream-bff" in u and "tag" in u)
                    or ("fhaicoreapps" in u and "tag" in u)
                    or (req.method in ("POST", "PUT", "PATCH", "DELETE")
                        and ("tag" in u or "document" in u) and "meltwater" in u)
                )
                if interesting:
                    try:
                        hdrs = await req.all_headers()
                    except Exception:
                        hdrs = {}
                    with open(cap_file, "a", encoding="utf-8") as f:
                        f.write(f"REQUEST {req.method} {req.url}\n"
                                f"headers={json.dumps(hdrs)}\nbody={req.post_data or ''}\n"
                                + "-" * 80 + "\n")
            except Exception:
                pass
        ctx.on("request", _capture)

        async def _capture_resp(resp):
            # Grab tag-related BFF responses — the tag LIST (name -> internal id)
            # comes back here, which is exactly what api_apply needs to resolve
            # tag names to the tagIds the enqueue endpoint expects.
            try:
                u = resp.url
                if "content-stream-bff" in u and "tag" in u.lower() and "enqueue" not in u:
                    body = await resp.text()
                    if "tag" in body.lower():
                        with open(cap_file, "a", encoding="utf-8") as f:
                            f.write(f"RESPONSE {resp.status} {u}\nbody={body[:60000]}\n"
                                    + "-" * 80 + "\n")
            except Exception:
                pass
        ctx.on("response", _capture_resp)

        if capture_only:
            if not config.HEADLESS:
                await page.goto(config.MELTWATER_URL)
            print(f"\nCAPTURE MODE — capturing to {cap_file}", flush=True)
            input(">>> In the opened window: open any article's Tag modal and APPLY one tag "
                  "(you can remove it after). That captures the tag list + request headers. "
                  "Then press Enter here to finish...\n")
            await ctx.close()
            print("Capture done. Now run:  python -m brands.bentley.api_apply "
                  "decisions_bentley.json --capture mw_capture.log", flush=True)
            return

        if not config.HEADLESS:
            await page.goto(config.MELTWATER_URL)
            input("\n>>> In the NEW browser window Playwright opened, log into Meltwater, open the "
                  "'Bentley 2026 | All Coverage' feed (set the date range to cover the export dates), "
                  "then press Enter here...\n")

        print(f"Walking the feed to match {len(want)} item(s)...  (Ctrl-C to stop)", flush=True)
        seen_keys, rounds, no_progress = set(), 0, 0
        MAX_ROUNDS = 150
        while rounds < MAX_ROUNDS and len(done) < len(want) and no_progress < 5:
            rounds += 1
            cards = await page.query_selector_all(CARD_SEL)
            matched_this_round = 0
            for card in cards:
                try:
                    key = await get_card_permalink(card)
                except Exception:
                    continue
                if key:
                    seen_keys.add(key)
                if not key or key in done or key not in want:
                    continue
                plan = want[key]
                done.add(key)
                matched_this_round += 1
                a, s, f = await apply_plan_to_card(page, card, plan["to_add"],
                                                   dry_run=not apply_changes, delay=delay)
                applied_all += [(key, t) for t in a]
                skipped_all += [(key, t) for t in s]
                failed_all += [(key, t) for t in f]
                print(("[WALK] " if not apply_changes else "[APPLY] ") +
                      f"{key[:58]}  +{a}" + (f" skip{s}" if s else "") + (f" FAIL{f}" if f else ""),
                      flush=True)
            print(f"  round {rounds}: {len(cards)} cards on screen, "
                  f"{len(done)}/{len(want)} matched, {len(seen_keys)} unique articles seen so far",
                  flush=True)
            moved = await _scroll(page)
            await asyncio.sleep(1.0)
            no_progress = 0 if (matched_this_round or moved) else no_progress + 1

        # Diagnostic when matching is poor — show feed keys vs what we wanted.
        if len(done) < len(want):
            print("\n[diagnostic] a few ARTICLE URLS SEEN in the feed:", flush=True)
            for k in list(seen_keys)[:8]:
                print("   feed:", k, flush=True)
            print("[diagnostic] a few URLS WE WANTED to match:", flush=True)
            for k in list(want)[:8]:
                print("   want:", k, flush=True)

        await ctx.close()

    print("\n" + "=" * 70)
    print(("WALK-ONLY (nothing applied)" if not apply_changes else "LIVE APPLY") + " — done")
    print(f"matched items: {len(done)}/{len(want)} | tags applied: {len(applied_all)} | "
          f"already-present skipped: {len(skipped_all)} | failed: {len(failed_all)}")
    unmatched = [p['url'] for k, p in want.items() if k not in done]
    if unmatched:
        print(f"NOT found in feed ({len(unmatched)}): " + ", ".join(u[:50] for u in unmatched[:10]))


def main():
    ap = argparse.ArgumentParser(description="Bentley Phase-2 apply (plan / dry-run / live).")
    ap.add_argument("decisions", help="decisions JSON from classify_batch")
    ap.add_argument("--out", default="", help="also write the plan to this JSON path")
    ap.add_argument("--live", action="store_true",
                    help="open a browser and match against the live Meltwater feed")
    ap.add_argument("--apply", action="store_true",
                    help="with --live: actually APPLY tags (default is walk-only, applies nothing)")
    ap.add_argument("--capture-only", action="store_true",
                    help="open the browser and capture one manual tag (headers + tag-id list) to "
                         "mw_capture.log for api_apply; applies nothing itself")
    args = ap.parse_args()

    decisions = load_decisions(args.decisions)
    plans = build_plan(decisions)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(plans, f, indent=2, ensure_ascii=False)
        print(f"Wrote plan -> {args.out}")

    if args.capture_only:
        import asyncio
        asyncio.run(run_live(plans, apply_changes=False, capture_only=True))
        return

    if not args.live:
        print_dry_run(plans)
        return

    import asyncio
    if args.apply:
        print("⚠ LIVE APPLY: this will write tags to the shared Meltwater account.")
    else:
        print("WALK-ONLY: matching cards on the live feed; NOTHING will be applied.")
    asyncio.run(run_live(plans, apply_changes=args.apply))


if __name__ == "__main__":
    main()
