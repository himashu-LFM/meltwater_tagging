"""
Phase 2 — Apply.

Drives the Meltwater Explore UI with Playwright and applies the precomputed
tags from decisions.json. No LLM in this loop — it just mechanically performs
the skill's UI flow, fast:

  hover card -> tag icon -> "Tag content" modal -> Find -> check tag -> Apply

Safety rules preserved from the skill:
  * Reads each post's TRUE tag state from the "Remove [tag]" chip; SKIPS any
    post already carrying a sentiment tag (never re-judges/overrides).
  * Expands each "Similar" group exactly once (detects state via aria-label).
  * Never creates tags, never exports, never touches account/search settings.
  * Only applies tags this script was told to apply (action == apply).
  * 'review' / 'skip_flag' / 'paywall' decisions are reported, never tagged.

First run opens a real browser window using a persistent profile — log into
Meltwater and open the topic feed, then return to the terminal and press Enter.

Usage:
    python apply_tags.py [--decisions decisions.json] [--dry-run]

NOTE: Meltwater's DOM is not public, so the selectors below are built from the
aria-labels documented in SKILL.md. If a selector misses, adjust the SELECTORS
block — the structure (find card -> read chip -> open modal -> apply) holds.
"""

import argparse
import asyncio
import json
import re

from playwright.async_api import async_playwright

import config
from results_writer import write_results_excel

# Selectors — the hover-toolbar + "Tag content" modal were confirmed live from
# screenshots (2026-07-10). The tag button shows tooltip "Tag" (MUI price-tag,
# LocalOffer/Sell icon); the modal is titled "Tag content" with a "Find" box,
# checkbox rows per tag ("Negative - Kaseya"), and Close/Apply buttons.
SELECTORS = {
    "open_article": '[aria-label*="Open article in new tab"]',
    # tag (price-tag) action icon that appears on hover -- match tooltip/label
    # "Tag" and the common MUI tag icons as fallbacks
    "tag_icon": (
        '[aria-label="Tag"], [title="Tag"], [aria-label*="tag" i], '
        'button:has([data-testid="LocalOfferIcon"]), button:has([data-testid="SellIcon"]), '
        'button:has([data-testid="LocalOfferOutlinedIcon"])'
    ),
    "similar_collapsed": '[aria-label="Open similar articles"]',
    "modal": 'text="Tag content"',
    "find_box": 'input[placeholder*="Find" i], input[aria-label*="Find" i]',
    "apply_btn": 'button:has-text("Apply")',
}


def norm_permalink(url: str) -> str:
    """Canonical dedup/match key.

    For Reddit, the SAME post has multiple equivalent URL forms — e.g. a user
    post is reachable as both /user/<name>/comments/<id>/... and
    /r/u_<name>/comments/<id>/... — so string comparison of the full URL fails.
    The post id (after /comments/) plus the comment id (after /comment/) is the
    stable unique key, so we canonicalize Reddit URLs to that.
    """
    if not url:
        return ""
    u = re.sub(r"^https?://(www\.)?", "", url.strip())
    u = u.split("?")[0].split("#")[0]

    if "reddit.com" in u:
        m = re.search(r"/comments/([a-z0-9]+)", u, re.I)
        if m:
            key = "reddit:" + m.group(1).lower()
            cm = re.search(r"/comment/([a-z0-9]+)", u, re.I)
            if cm:
                key += "/" + cm.group(1).lower()
            return key

    return u.rstrip("/").lower()


async def get_card_permalink(card):
    # Prefer a real article link (the source URL). The "Open article in new tab"
    # control's href is most reliable; fall back to any reddit/http anchor in the
    # card. Confirmed live: cards contain <a href="https://www.reddit.com/...">.
    link = await card.query_selector(SELECTORS["open_article"])
    if link:
        href = await link.get_attribute("href")
        if href:
            return norm_permalink(href)
    for sel in ('a[href*="reddit.com"]', 'a[href^="http"]'):
        a = await card.query_selector(sel)
        if a:
            href = await a.get_attribute("href")
            if href and "meltwater.com" not in href:
                return norm_permalink(href)
    return ""


async def card_existing_tag(card):
    """Return the tag name if the card shows a 'Remove [tag]' chip, else None."""
    chips = await card.query_selector_all('[aria-label^="Remove "]')
    for chip in chips:
        label = await chip.get_attribute("aria-label")
        # Account tags look like "Remove Kaseya - positive"; also tolerate the
        # reverse order just in case.
        if label and re.search(
            r"Remove .+-\s*(positive|negative|neutral)\b|Remove (positive|negative|neutral)\s*-",
            label, re.I,
        ):
            return label.replace("Remove ", "", 1).strip()
    return None


async def expand_similar_if_needed(card):
    toggle = await card.query_selector(SELECTORS["similar_collapsed"])
    if toggle:
        await toggle.click()
        await asyncio.sleep(0.4)


async def _log_card_buttons(card, note=""):
    """Diagnostic: after a real hover, dump every button/icon in the card so we
    can identify the tag icon's real selector. Logs via print so it shows in
    both the CLI and the web app's captured stdout."""
    try:
        data = await card.evaluate("""el => {
            const out = [];
            el.querySelectorAll('button,[role="button"],a,[aria-label]').forEach(b => {
                out.push({
                    label: b.getAttribute('aria-label'),
                    title: b.getAttribute('title'),
                    icon: b.querySelector('svg') ? b.querySelector('svg').getAttribute('data-testid') : null,
                    text: (b.textContent || '').trim().slice(0, 18),
                });
            });
            return out;
        }""")
        print(f"[card-buttons {note}] {data}", flush=True)
    except Exception as e:
        print(f"[card-buttons {note}] failed: {e}", flush=True)


def normalize_tag(tag: str) -> str:
    """Rebuild a tag string as 'Sentiment - Brand' (the live Meltwater format),
    regardless of the stored order. Old classification runs saved the tag as
    'Kaseya - neutral' (brand-first); this makes those apply correctly too."""
    if not tag:
        return tag
    parts = [p.strip() for p in re.split(r"\s*-\s*", tag) if p.strip()]
    sent = None
    brand = []
    for p in parts:
        if p.lower() in ("positive", "negative", "neutral"):
            sent = p.capitalize()
        else:
            brand.append(p)
    if sent and brand:
        return f"{sent} - {' - '.join(brand)}"
    return tag


async def _close_any_modal(page):
    """Dismiss an open MUI dialog so its overlay stops intercepting clicks."""
    try:
        dlg = await page.query_selector('.MuiDialog-container, [role="dialog"]')
        if not dlg:
            return
        close = await page.query_selector('button:has-text("Close"), [aria-label="Close"], button[aria-label*="close" i]')
        if close:
            await close.click()
        else:
            await page.keyboard.press("Escape")
        await asyncio.sleep(0.3)
    except Exception:
        try:
            await page.keyboard.press("Escape")
        except Exception:
            pass


async def apply_tag_to_card(page, card, tag, dry_run, delay):
    """Run the modal flow to apply one tag to one card. Returns True on success.
    Always closes the modal before returning (even on failure) so a stuck dialog
    can't block the next card."""
    tag = normalize_tag(tag)

    # Make sure no leftover dialog is covering the feed before we hover.
    await _close_any_modal(page)

    await card.hover()
    await asyncio.sleep(max(delay, 0.6))  # let the hover toolbar render
    tag_icon = await card.query_selector(SELECTORS["tag_icon"])
    if not tag_icon:
        await _log_card_buttons(card, note="tag-icon-not-found")
        return False
    if dry_run:
        return True

    try:
        await tag_icon.click()
        dialog = await page.wait_for_selector(SELECTORS["modal"], timeout=8000)
        # Scope all modal interactions to the dialog container so we never grab
        # the page's global "Find" search box or unrelated elements.
        dlg = await page.query_selector('.MuiDialog-container, [role="dialog"]')
        scope = dlg or page
        await asyncio.sleep(delay)

        # Type the tag into the modal's Find box (scoped to the dialog).
        find = await scope.query_selector('input[placeholder*="Find" i], input[type="text"], input:not([type])')
        if find:
            await find.fill(tag)
            await asyncio.sleep(max(delay, 0.6))

        # Select the matching tag row within the dialog.
        selected = False
        try:
            row = scope.get_by_text(tag, exact=True) if hasattr(scope, "get_by_text") else None
            if row is not None and await row.count() > 0:
                await row.first.click()
                selected = True
        except Exception:
            pass
        if not selected:
            cb = await scope.query_selector(
                f'label:has-text("{tag}") input[type="checkbox"], '
                f'input[type="checkbox"][aria-label*="{tag}"]'
            )
            if cb:
                await cb.check()
                selected = True
        if not selected:
            # last resort: click the text node inside the dialog
            node = await scope.query_selector(f'text="{tag}"')
            if node:
                await node.click()
                selected = True

        if not selected:
            print(f"[tag-modal] could not find the tag row for {tag!r} — dumping visible rows", flush=True)
            try:
                rows = await scope.evaluate("""el => [...el.querySelectorAll('input[type=checkbox]')]
                    .map(c => (c.closest('label,li,div')||{}).innerText || '').filter(Boolean).slice(0,25)""")
                print(f"[tag-modal] visible rows: {rows}", flush=True)
            except Exception:
                pass
            return False

        await asyncio.sleep(delay)
        apply_btn = await scope.query_selector('button:has-text("Apply")')
        if apply_btn:
            await apply_btn.click()
            await asyncio.sleep(max(delay, 0.8))

        # Confirm via a "Remove [tag]" chip (card may be stale -> page fallback).
        for target in (card, page):
            try:
                chip = await target.query_selector(f'[aria-label="Remove {tag}"]') \
                    or await target.query_selector(f'[aria-label*="{tag}"]')
                if chip is not None:
                    return True
            except Exception:
                continue
        # Applied but couldn't confirm the chip -> assume success if Apply clicked.
        return apply_btn is not None
    finally:
        await _close_any_modal(page)


async def run(decisions_path, dry_run):
    with open(decisions_path, encoding="utf-8") as f:
        data = json.load(f)

    run_brand = data["run_brand"]
    # map permalink -> tag to apply
    to_apply = {norm_permalink(d["permalink"]): d["tag"] for d in data["decisions"] if d.get("tag")}
    decisions_by_link = {norm_permalink(d["permalink"]): d for d in data["decisions"]}

    applied, skipped_already, failed = [], [], []
    seen = set()

    delay = config.ACTION_DELAY_MS / 1000.0

    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            config.USER_DATA_DIR, headless=config.HEADLESS
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        if not config.HEADLESS:
            await page.goto(config.MELTWATER_URL)
            input(
                f"\n>>> Log into Meltwater and open the '{run_brand}' topic feed in the "
                "browser window, then press Enter here to start tagging...\n"
            )

        # Disciplined top-to-bottom pass over the virtualized feed.
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
                    continue  # review / skip_flag / paywall — handled in report

                ok = await apply_tag_to_card(page, card, tag, dry_run, delay)
                rec = {"permalink": permalink, "tag": tag,
                       "reason": decisions_by_link.get(permalink, {}).get("reason", "")}
                (applied if ok else failed).append(rec)
                print(("[DRY] " if dry_run else "") + f"{'OK ' if ok else 'FAIL'} {tag}  {permalink}")

            # scroll to load more virtualized rows
            await page.mouse.wheel(0, 4000)
            await asyncio.sleep(0.8)
            if len(seen) == last_seen_count:
                stable_rounds += 1
            else:
                stable_rounds = 0
            last_seen_count = len(seen)

        await ctx.close()

    write_report(data, applied, skipped_already, failed, seen, decisions_by_link, dry_run)


def write_report(data, applied, skipped_already, failed, seen, decisions_by_link, dry_run):
    run_brand = data["run_brand"]
    lines = [f"# Meltwater Sentiment Tagging Report — {run_brand}", ""]
    if dry_run:
        lines.append("_DRY RUN — no tags were actually applied._\n")

    lines.append(f"## APPLIED tags ({len(applied)})")
    for r in applied:
        lines.append(f"- {r['tag']}: {r['reason']}  \n  {r['permalink']}")

    # Untagged-by-this-skill, for any reason.
    untagged = []
    for d in data["decisions"]:
        link = d["permalink"]
        if d.get("tag"):
            continue
        action = d.get("action")
        if action == "skip_flag":
            untagged.append((link, f"off-topic-brand (flag for {d.get('flag_brand','?')})", d.get("reason", "")))
        elif action == "review":
            untagged.append((link, "Review Needed (ambiguous)", d.get("reason", "")))
        elif action == "paywall":
            untagged.append((link, "paywall (unreadable)", d.get("reason", "")))
    # failures during apply
    for r in failed:
        untagged.append((r["permalink"], "apply-failed (could not tag in UI)", r["reason"]))
    # decisions whose card was never reached in the feed
    reached = seen
    for d in data["decisions"]:
        if d.get("tag") and norm_permalink(d["permalink"]) not in reached:
            untagged.append((d["permalink"], "unreachable (not found in feed)", d.get("reason", "")))

    lines.append(f"\n## HIGHLIGHTED — UNTAGGED POSTS ({len(untagged)})")
    for link, reason, detail in untagged:
        lines.append(f"- [{reason}] {link}" + (f" — {detail}" if detail else ""))

    lines.append(f"\n## Already-tagged (skipped, untouched) ({len(skipped_already) + len(data['already_tagged'])})")
    for r in data["already_tagged"]:
        lines.append(f"- {r['permalink']} ({r.get('existing_tags','')})")
    for r in skipped_already:
        lines.append(f"- {r['permalink']} ({r.get('existing_tags','')})")

    # reconciliation
    total = data.get("export_count", "?")
    lines.append("\n## Reconcile")
    lines.append(f"- export count: {total}")
    lines.append(f"- applied: {len(applied)}")
    lines.append(f"- already-tagged (skipped): {len(skipped_already) + len(data['already_tagged'])}")
    lines.append(f"- untagged-highlighted: {len(untagged)}")

    with open(config.REPORT_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    # Excel results (results/ folder, one timestamped file per run).
    rows = []
    for r in applied:
        rows.append({"status": "applied", "tag": r["tag"], "reason": r["reason"],
                     "permalink": r["permalink"]})
    for link, reason, detail in untagged:
        rows.append({"status": "untagged", "tag": "", "reason": f"{reason}"
                     + (f" — {detail}" if detail else ""), "permalink": link})
    for r in data["already_tagged"]:
        rows.append({"status": "already-tagged (skipped)", "tag": r.get("existing_tags", ""),
                     "reason": "pre-existing tag, untouched", "permalink": r["permalink"]})
    for r in skipped_already:
        rows.append({"status": "already-tagged (skipped)", "tag": r.get("existing_tags", ""),
                     "reason": "pre-existing tag, untouched", "permalink": r["permalink"]})
    xlsx = write_results_excel(rows, kind="tagging_results", brand=run_brand)

    print(f"\nReport written -> {config.REPORT_FILE}")
    print(f"Excel results  -> {xlsx}")
    print(f"Applied: {len(applied)} | already-tagged: "
          f"{len(skipped_already)+len(data['already_tagged'])} | untagged: {len(untagged)} | failed: {len(failed)}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--decisions", default=config.DECISIONS_FILE)
    ap.add_argument("--dry-run", action="store_true", help="Walk the feed and report, but apply nothing.")
    args = ap.parse_args()
    asyncio.run(run(args.decisions, args.dry_run))


if __name__ == "__main__":
    main()
