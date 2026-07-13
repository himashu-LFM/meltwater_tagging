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
            post_id = m.group(1).lower()
            key = "reddit:" + post_id
            # A comment id can appear in two URL forms Reddit uses interchangeably:
            #   .../comments/<post>/comment/<cid>/          (current form)
            #   .../comments/<post>/<title-slug>/<cid>/     (older/redirect form)
            # Handle both: prefer the explicit /comment/<cid>, else take a
            # trailing base36 id segment that sits after the title slug.
            cm = re.search(r"/comment/([a-z0-9]+)", u, re.I)
            if cm:
                key += "/" + cm.group(1).lower()
            else:
                after = u[m.end():]  # path after "/comments/<post_id>"
                segs = [s for s in after.split("/") if s]
                # segs[0] is the title slug (if present); a real comment id is a
                # short base36 token with no underscores following that slug.
                if len(segs) >= 2 and re.fullmatch(r"[a-z0-9]{4,}", segs[-1], re.I):
                    key += "/" + segs[-1].lower()
            return key

    return u.rstrip("/").lower()


def reddit_post_id(key: str) -> str:
    """Post id from a canonical norm_permalink key.
    'reddit:1up7akg/ow12mxr' -> '1up7akg'; 'reddit:1upljed' -> '1upljed'.
    Returns '' for non-Reddit keys so post-level fallback never fires for them."""
    if not key or not key.startswith("reddit:"):
        return ""
    return key[len("reddit:"):].split("/", 1)[0]


def build_post_fallback(to_apply: dict) -> dict:
    """Map Reddit post-id -> its single (target_key, value) entry, but ONLY for
    post ids that have exactly one target in `to_apply`.

    Why: the analyst's source URL and Meltwater's indexed mention often differ in
    granularity — the analyst may give a specific comment URL while Meltwater
    surfaces the parent post as the mention (or vice versa). Matching on the post
    id recovers those. Restricting to single-target post ids means we NEVER have
    to guess which tag to use when a post has multiple distinct targets (e.g. two
    different comments), so bulk runs can't mis-tag. Post ids are globally unique,
    so a fallback entry can only ever resolve to that post's own target."""
    from collections import defaultdict
    by_post = defaultdict(list)
    for key, val in to_apply.items():
        pid = reddit_post_id(key)
        if pid:
            by_post[pid].append((key, val))
    return {pid: lst[0] for pid, lst in by_post.items() if len(lst) == 1}


def resolve_target(card_key: str, to_apply: dict, post_fallback: dict):
    """Resolve a feed card's canonical key to the target we should act on.
    Returns (target_key, value) or (None, None). Exact match always wins; the
    Reddit post-id fallback only covers the comment/post granularity mismatch and
    only for unambiguous single-target posts (see build_post_fallback)."""
    if card_key in to_apply:
        return card_key, to_apply[card_key]
    pid = reddit_post_id(card_key)
    if pid and pid in post_fallback:
        return post_fallback[pid]
    return None, None


async def get_card_permalink(card):
    """Return the card's canonical permalink key, preferring the MOST SPECIFIC
    Reddit link in the card.

    A single Meltwater card can contain several anchors — e.g. a post-level title
    link AND a comment-level "source" link. When the tracked mention is a comment,
    we must key on the comment, not the parent post: otherwise many separate
    comment-mentions of the same thread all collapse to one post-level key and
    become impossible to tell apart (exactly the '48 mentions on one post' case).
    So we gather every href in the card and pick the deepest Reddit link.
    """
    hrefs = []
    link = await card.query_selector(SELECTORS["open_article"])
    if link:
        h = await link.get_attribute("href")
        if h:
            hrefs.append(h)
    for a in await card.query_selector_all("a[href]"):
        h = await a.get_attribute("href")
        if h:
            hrefs.append(h)

    best, best_rank = "", -1
    for h in hrefs:
        if "meltwater.com" in h:
            continue
        key = norm_permalink(h)
        if not key:
            continue
        # reddit comment (post/comment) > reddit post > any other http link
        if key.startswith("reddit:") and "/" in key[len("reddit:"):]:
            rank = 3
        elif key.startswith("reddit:"):
            rank = 2
        else:
            rank = 1
        if rank > best_rank:
            best, best_rank = key, rank
    return best


async def log_card_links(card, note=""):
    """Diagnostic: dump every href + data-* attribute + a text snippet from a
    card, so we can see whether a comment-level URL or Meltwater Document ID is
    present anywhere in the DOM (needed to distinguish many comment-mentions of
    the same post). Printed with flush so it shows in captured stdout logs."""
    try:
        data = await card.evaluate("""el => {
            const links = [...el.querySelectorAll('a[href]')]
                .map(a => a.getAttribute('href'))
                .filter(h => h && h.indexOf('meltwater.com') === -1)
                .slice(0, 25);
            const attrs = {};
            const grab = (n) => {
                for (const at of (n.attributes || [])) {
                    if (at.name.startsWith('data-') && Object.keys(attrs).length < 40)
                        attrs[at.name] = String(at.value).slice(0, 80);
                }
            };
            grab(el);
            el.querySelectorAll('*').forEach(grab);
            return { links, attrs, text: (el.innerText || '').replace(/\\s+/g, ' ').slice(0, 180) };
        }""")
        print(f"[card-links {note}] links={data['links']}", flush=True)
        print(f"[card-links {note}] data-attrs={data['attrs']}", flush=True)
        print(f"[card-links {note}] text={data['text']!r}", flush=True)
    except Exception as e:
        print(f"[card-links {note}] failed: {e}", flush=True)


async def card_existing_tag(card):
    """Return the tag name if the card VISIBLY shows a 'Remove [Sentiment - Brand]'
    chip, else None.

    Only VISIBLE chips count. A genuinely-applied tag renders a visible removable
    chip on the card face (that is exactly how apply_tag_to_card confirms success).
    A matching chip that is present in the DOM but hidden almost always belongs to
    a collapsed 'Similar articles' duplicate, an off-card control, or a filter
    pill — counting those produced FALSE 'already tagged' skips (a card the analyst
    can't see any tag on gets skipped). We log every candidate + visibility so a
    false match is diagnosable."""
    chips = await card.query_selector_all('[aria-label^="Remove "]')
    candidates = []
    hit = None
    for chip in chips:
        label = await chip.get_attribute("aria-label")
        if not label or not re.search(
            r"Remove .+-\s*(positive|negative|neutral)\b|Remove (positive|negative|neutral)\s*-",
            label, re.I,
        ):
            continue
        try:
            vis = await chip.is_visible()
        except Exception:
            vis = False
        candidates.append((label, vis))
        if vis and hit is None:
            hit = label.replace("Remove ", "", 1).strip()
    if candidates:
        print(f"[existing-tag] candidates={candidates} -> {hit!r}", flush=True)
    return hit


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

        # Select the matching tag row within the dialog. Prefer a checkbox and
        # use .check() (idempotent: it ENSURES checked and is a no-op if the tag
        # is already applied) so we never accidentally TOGGLE OFF an existing tag.
        # Only fall back to clicking the row/text when no checkbox is present.
        selected = False
        cb = await scope.query_selector(
            f'label:has-text("{tag}") input[type="checkbox"], '
            f'input[type="checkbox"][aria-label*="{tag}"]'
        )
        if cb:
            try:
                if await cb.is_checked():
                    # already applied — leave it as-is, count as success (never toggle off)
                    print(f"[tag-modal] {tag!r} already checked in modal — not toggling", flush=True)
                    return True
            except Exception:
                pass
            try:
                await cb.check()
                selected = True
            except Exception:
                pass
        if not selected:
            try:
                row = scope.get_by_text(tag, exact=True) if hasattr(scope, "get_by_text") else None
                if row is not None and await row.count() > 0:
                    await row.first.click()
                    selected = True
            except Exception:
                pass
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
    # Post-id fallback so a comment-URL target still matches a post-level card
    # (and vice versa), safely limited to unambiguous single-target posts.
    post_fallback = build_post_fallback(to_apply)

    applied, skipped_already, failed = [], [], []
    seen = set()
    applied_keys = set()   # target keys reached a final decision on

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

                target_key, tag = resolve_target(permalink, to_apply, post_fallback)
                if not tag or target_key in applied_keys:
                    continue  # review / skip_flag / paywall / already handled

                # Read the card's own visible tags BEFORE expanding "Similar"
                # (expansion pulls a duplicate's tags in -> false "already tagged").
                existing = await card_existing_tag(card)
                if existing:
                    skipped_already.append({"permalink": permalink, "existing_tags": existing})
                    applied_keys.add(target_key)
                    continue

                await expand_similar_if_needed(card)

                if target_key != permalink:
                    print(f"[match] card {permalink} -> target {target_key} (post-id fallback)")

                ok = await apply_tag_to_card(page, card, tag, dry_run, delay)
                rec = {"permalink": target_key, "tag": tag,
                       "reason": decisions_by_link.get(target_key, {}).get("reason", "")}
                (applied if ok else failed).append(rec)
                applied_keys.add(target_key)
                print(("[DRY] " if dry_run else "") + f"{'OK ' if ok else 'FAIL'} {tag}  {target_key}")

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
