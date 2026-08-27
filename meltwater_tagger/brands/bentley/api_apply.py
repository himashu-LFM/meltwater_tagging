"""
Bentley Phase-2 apply via Meltwater's document-tagging API (by Document ID).

Confirmed from a real captured call (2026-08-26):
  POST https://bff.fhaicoreapps.com/prd-flux-content-stream-bff/tags/enqueue-document-tagging
  body: {"documents":[{"documentId":"<id>","matchSentence":"...","keywords":[...]}],
         "tagIds":[<int>, ...]}
  -> tags are passed by INTERNAL ID (e.g. "Not in scope" = 2301302), and tagIds
     is an ARRAY, so ALL of a document's tags go in ONE call. No feed scrolling.

Because tags are ids, we need a NAME->ID map. It is recovered from the tag-list
response the Meltwater UI fetches (captured to mw_capture.log by apply_bentley's
--live run), or supplied as a JSON file via --tag-map.

The live sender runs the POST from INSIDE the authenticated browser page
(page.evaluate + fetch), reading the Auth0 access token from the page's own
localStorage — so we never reconstruct headers or handle the token in Python. It
is gated (dry-run by default), throttled, and refuses to send unmapped tags.

CLI:
  # dry-run (no browser); resolve tags to ids using a captured/known map:
  python -m brands.bentley.api_apply decisions_bentley.json --capture mw_capture.log
  # live (opens browser, log in, applies by document id), throttled:
  python -m brands.bentley.api_apply decisions_bentley.json --capture mw_capture.log --live --apply
"""

import argparse
import json
import re
import time

from brands.bentley import apply_bentley

ENDPOINT = ("https://bff.fhaicoreapps.com/prd-flux-content-stream-bff/"
            "tags/enqueue-document-tagging")

# Tag ids learned from captures (seed). The rest come from the tag-list capture
# or a --tag-map file. Keep any confirmed ids here as a fallback.
KNOWN_TAG_IDS = {
    "Not in scope": 2301302,
}


# ---------------------------------------------------------------------------
# TAG NAME -> ID MAP
# ---------------------------------------------------------------------------
def _norm(name: str) -> str:
    """Loose key for matching tag names across separator/spacing/case variants
    (Meltwater's own list may differ slightly from our canonical strings)."""
    return re.sub(r"[\s|/-]+", " ", (name or "").lower()).strip()


def build_tag_map_from_capture(logfile: str) -> dict:
    """Extract a {tag_name: tag_id} map from any tag-list JSON captured in the
    log. Scans for objects that pair a name-ish field with an id-ish field."""
    try:
        text = open(logfile, encoding="utf-8", errors="replace").read()
    except Exception:
        return {}
    out = {}
    # Pull every {... "name/title/label": "...", "id/tagId": <int> ...} pair.
    for blk in text.split("-" * 80):
        if "RESPONSE" not in blk:
            continue
        body_m = re.search(r"body=(.*)", blk, re.S)
        if not body_m:
            continue
        body = body_m.group(1)
        # try structured parse first
        try:
            data = json.loads(body)
            _collect_tag_pairs(data, out)
        except Exception:
            # regex fallback: "...name...":"X" ... "id/tagId": N (in any order)
            for m in re.finditer(
                r'\{[^{}]*?"(?:name|title|label)"\s*:\s*"([^"]+)"[^{}]*?"(?:tagId|id)"\s*:\s*(\d+)',
                body):
                out[m.group(1)] = int(m.group(2))
            for m in re.finditer(
                r'\{[^{}]*?"(?:tagId|id)"\s*:\s*(\d+)[^{}]*?"(?:name|title|label)"\s*:\s*"([^"]+)"',
                body):
                out[m.group(2)] = int(m.group(1))
    return out


def _collect_tag_pairs(node, out: dict):
    if isinstance(node, dict):
        name = node.get("name") or node.get("title") or node.get("label")
        tid = node.get("tagId") or node.get("id")
        if isinstance(name, str) and isinstance(tid, int):
            out[name] = tid
        for v in node.values():
            _collect_tag_pairs(v, out)
    elif isinstance(node, list):
        for v in node:
            _collect_tag_pairs(v, out)


def _bundled_tag_map() -> dict:
    """The tag name->id map captured from Meltwater's tag list (brands/bentley/
    tag_ids.json), so a normal run needs no fresh capture. Refresh it by running
    apply_bentley --capture-only and re-parsing if Meltwater adds/renames tags."""
    import os
    path = os.path.join(os.path.dirname(__file__), "tag_ids.json")
    try:
        return {k: int(v) for k, v in json.load(open(path, encoding="utf-8")).items()}
    except Exception:
        return {}


def resolve_tag_map(capture: str = "", tag_map_file: str = "") -> dict:
    """Assemble the best available name->id map: file > capture > bundled > seeds."""
    m = dict(KNOWN_TAG_IDS)
    m.update(_bundled_tag_map())
    if capture:
        m.update(build_tag_map_from_capture(capture))
    if tag_map_file:
        try:
            m.update({k: int(v) for k, v in json.load(open(tag_map_file)).items()})
        except Exception as e:
            print(f"Could not read --tag-map {tag_map_file}: {e}")
    return m


def _lookup(name: str, tag_map: dict) -> int | None:
    if name in tag_map:
        return tag_map[name]
    norm = {_norm(k): v for k, v in tag_map.items()}
    return norm.get(_norm(name))


# ---------------------------------------------------------------------------
# MANIFEST — per document: the tagIds to apply (names resolved), plus any unmapped
# ---------------------------------------------------------------------------
def build_manifest(plans: list[dict], tag_map: dict) -> tuple[list[dict], list[dict], set]:
    manifest, skipped_docs, unmapped = [], [], set()
    for p in plans:
        if p.get("action") != "apply" or not p.get("to_add"):
            continue
        doc_id = (p.get("document_id") or "").strip()
        if not doc_id:
            skipped_docs.append({"url": p.get("url", ""), "reason": "no document_id"})
            continue
        ids, names, miss = [], [], []
        for name in p["to_add"]:
            tid = _lookup(name, tag_map)
            if tid is None:
                miss.append(name)
                unmapped.add(name)
            else:
                ids.append(tid)
                names.append(name)
        manifest.append({"document_id": doc_id, "url": p.get("url", ""),
                         "tag_ids": ids, "tag_names": names, "unmapped": miss})
    return manifest, skipped_docs, unmapped


def print_dry_run(manifest, skipped_docs, unmapped, tag_map):
    print("=" * 78)
    print("BENTLEY API APPLY — DRY RUN (no calls sent)")
    print("=" * 78)
    docs_ready = [m for m in manifest if m["tag_ids"]]
    print(f"endpoint: {ENDPOINT}")
    print(f"tag map: {len(tag_map)} name->id entries")
    print(f"documents with >=1 mappable tag: {len(docs_ready)}  |  "
          f"total tagIds to send: {sum(len(m['tag_ids']) for m in manifest)}")
    if unmapped:
        print(f"⚠ {len(unmapped)} tag name(s) have NO id yet (capture the tag list to map them):")
        for n in sorted(unmapped):
            print(f"    - {n}")
    if skipped_docs:
        print(f"⚠ {len(skipped_docs)} item(s) have no document_id and can't be sent.")
    print("-" * 78)
    for m in manifest:
        print(f"DOC {m['document_id']}  ->  tagIds {m['tag_ids']}")
        print(f"    {m['url'][:66]}")
        print(f"    names : {m['tag_names']}")
        if m["unmapped"]:
            print(f"    UNMAPPED (skipped): {m['unmapped']}")
    print("=" * 78)


# ---------------------------------------------------------------------------
# LIVE SENDER — POST from inside the authenticated browser page.
# ---------------------------------------------------------------------------
# The BFF wants a MELTWATER-issued JWT (payload has `company`/`user` claims),
# sent RAW (no "Bearer " prefix) — NOT the Auth0 SPA access token. Scan
# localStorage + sessionStorage (including tokens nested inside JSON values) for
# a 3-part JWT whose payload carries a `company` or `user` claim.
_TOKEN_JS = """
() => {
  function payloadOk(t) {
    if (typeof t !== 'string') return false;
    const p = t.split('.');
    if (p.length !== 3) return false;
    try {
      const pad = s => s + '='.repeat((4 - s.length % 4) % 4);
      const b = JSON.parse(atob(pad(p[1].replace(/-/g,'+').replace(/_/g,'/'))));
      return ('company' in b) || ('user' in b);
    } catch (e) { return false; }
  }
  function scan(store) {
    for (let i = 0; i < store.length; i++) {
      const v = store.getItem(store.key(i));
      if (!v) continue;
      if (payloadOk(v)) return v;
      try {
        const o = JSON.parse(v);
        const stack = [o];
        while (stack.length) {
          const cur = stack.pop();
          if (typeof cur === 'string') { if (payloadOk(cur)) return cur; continue; }
          if (cur && typeof cur === 'object') for (const k in cur) stack.push(cur[k]);
        }
      } catch (e) {}
    }
    return '';
  }
  return scan(localStorage) || scan(sessionStorage) || '';
}
"""


async def run_api_live(manifest, apply_changes: bool, throttle_s: float = 1.5,
                       extra_headers: dict | None = None):
    """Open the authenticated browser and POST one tagging call per document via
    Playwright's request API (context.request) — which is NOT subject to the
    browser's CORS sandbox, unlike an in-page fetch to the cross-origin BFF. The
    session cookies come from the context; we attach the Auth0 bearer token read
    from the page's localStorage (plus any captured extra headers).

    apply_changes=False => opens the browser and confirms the token is reachable,
    but sends NOTHING (safe check)."""
    import asyncio
    import config
    from playwright.async_api import async_playwright

    docs = [m for m in manifest if m["tag_ids"]]
    if not docs:
        print("Nothing to send (no documents with mappable tag ids).")
        return

    ok, failed = [], []
    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(config.USER_DATA_DIR, headless=config.HEADLESS)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        if not config.HEADLESS:
            await page.goto(config.MELTWATER_URL)
            input("\n>>> Log into Meltwater in the opened window, then press Enter to start...\n")

        # Grab the app's OWN live authorization header off a real BFF request —
        # far more robust than guessing where the token lives (it may be in JS
        # memory, a cookie, or IndexedDB). The Meltwater app hits this BFF
        # constantly, so a reload surfaces a fresh, valid header we reuse verbatim.
        captured = {"auth": None}

        async def _grab(req):
            if captured["auth"]:
                return
            try:
                if "fhaicoreapps.com" in req.url or "content-stream-bff" in req.url:
                    h = await req.all_headers()
                    a = h.get("authorization")
                    if a:
                        captured["auth"] = a
            except Exception:
                pass
        ctx.on("request", _grab)

        # storage scan as a first try (cheap); else nudge the app with a reload.
        try:
            captured["auth"] = captured["auth"] or (await page.evaluate(_TOKEN_JS)) or None
        except Exception:
            pass
        if not captured["auth"]:
            try:
                await page.reload(wait_until="domcontentloaded")
            except Exception:
                pass
            for _ in range(15):
                if captured["auth"]:
                    break
                await asyncio.sleep(1.0)

        token = captured["auth"]
        if not token:
            print("⚠ Could not obtain a Meltwater auth header. In the window, open the Explore feed "
                  "(so the app talks to its API), then re-run.")
            await ctx.close()
            return
        print(f"got live auth header (len {len(token)}); sending via context.request (CORS-free)...",
              flush=True)

        # The BFF expects the RAW token in the authorization header (as the app
        # sends it) plus the app origin/referer (it checks cross-site origin).
        headers = {
            "authorization": token,
            "content-type": "application/json",
            "origin": "https://app.meltwater.com",
            "referer": "https://app.meltwater.com/",
        }
        if extra_headers:
            headers.update(extra_headers)

        for i, m in enumerate(docs, 1):
            body = {"documents": [{"documentId": m["document_id"]}], "tagIds": m["tag_ids"]}
            if not apply_changes:
                print(f"[{i}/{len(docs)}] DRY (browser open, token ok, not sending) "
                      f"{m['document_id']} -> {m['tag_ids']}", flush=True)
                continue
            try:
                resp = await ctx.request.post(ENDPOINT, headers=headers, data=json.dumps(body))
                text = ""
                try:
                    text = (await resp.text())[:300]
                except Exception:
                    pass
                if resp.ok:
                    ok.append(m["document_id"])
                    print(f"[{i}/{len(docs)}] OK  {m['document_id']} -> {m['tag_ids']} "
                          f"(status {resp.status})", flush=True)
                else:
                    failed.append((m["document_id"], resp.status, text))
                    print(f"[{i}/{len(docs)}] FAIL {m['document_id']} status={resp.status} {text[:140]}",
                          flush=True)
            except Exception as e:
                failed.append((m["document_id"], "exc", str(e)))
                print(f"[{i}/{len(docs)}] ERROR {m['document_id']}: {e}", flush=True)
            await asyncio.sleep(max(0.0, throttle_s))  # throttle the shared account

        await ctx.close()

    print("\n" + "=" * 60)
    print(("LIVE APPLY" if apply_changes else "TOKEN-CHECK (nothing sent)") + " — done")
    print(f"documents ok: {len(ok)} | failed: {len(failed)}")
    if failed:
        for d, s, t in failed[:10]:
            print(f"  FAIL {d} status={s} {str(t)[:100]}")


def main():
    ap = argparse.ArgumentParser(description="Bentley Phase-2 apply via document-tagging API.")
    ap.add_argument("decisions", help="decisions JSON from classify_batch")
    ap.add_argument("--capture", default="", help="mw_capture.log to build the tag name->id map")
    ap.add_argument("--tag-map", default="", help="JSON {tag_name: id} to supply/override the map")
    ap.add_argument("--live", action="store_true", help="open the browser to send calls")
    ap.add_argument("--apply", action="store_true", help="with --live: actually POST (default: token-check only)")
    ap.add_argument("--throttle", type=float, default=1.5, help="seconds between calls (default 1.5)")
    args = ap.parse_args()

    decisions = apply_bentley.load_decisions(args.decisions)
    plans = apply_bentley.build_plan(decisions)
    tag_map = resolve_tag_map(args.capture, args.tag_map)
    manifest, skipped_docs, unmapped = build_manifest(plans, tag_map)

    print_dry_run(manifest, skipped_docs, unmapped, tag_map)

    if args.live:
        import asyncio
        if unmapped and args.apply:
            print(f"\nNote: {len(unmapped)} unmapped tag(s) will be skipped this run.")
        asyncio.run(run_api_live(manifest, apply_changes=args.apply, throttle_s=args.throttle))


if __name__ == "__main__":
    main()
