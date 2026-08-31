"""
Bentley Phase-1 classifier (standalone runner).

Ties the Bentley pieces together for ONE article:
  1. deterministic source block-list      (rules.blocked_source)
  2. fetch the article text                (fetcher.fetch_article)
  3. ask Claude for scope + all tags       (prompts.SYSTEM_PROMPT + schema)
  4. apply deterministic post-rules        (rules.enforce_structural_rules)
  5. flag missing mandatory families       (rules.missing_mandatory)

This is intentionally self-contained so we can prove the classify logic on a
real URL before wiring it into the shared engine (classify.py). Later, Track B
lifts steps 2-4 into the engine as the Bentley profile's hooks.

CLI:
    python -m brands.bentley.classify_bentley <url> \
        [--source NAME] [--country COUNTRY] [--byline AUTHOR]
"""

import argparse
import json
import sys
from urllib.parse import urlparse

from anthropic import Anthropic

import config
from brands.bentley import prompts, rules, taxonomy
from brands.bentley.fetcher import fetch_article
from brands.bentley.live_rules import rules_block

_SINGLE = ["type_of_publication", "type_of_coverage", "region"]
_MULTI = ["corporate", "pillar", "industry", "product", "spokesperson"]


def _domain(url: str) -> str:
    """Bare outlet domain (no www.) — a strong signal for region/publication."""
    try:
        return urlparse(url).netloc.replace("www.", "")
    except Exception:
        return ""


def _to_families(d: dict) -> dict:
    """Flatten the model's flat decision into {family: [labels]}."""
    fam = {}
    for k in _SINGLE:
        v = (d.get(k) or "").strip()
        fam[k] = [v] if v and v != "Not in scope" else []
    for k in _MULTI:
        fam[k] = [x for x in (d.get(k) or []) if x]
    return fam


def _flatten(fam: dict) -> list[str]:
    out = []
    for k in _SINGLE + _MULTI:
        out.extend(fam.get(k, []))
    return out


def _snippet_text(headline: str, snippet: str) -> str:
    """Combine a Meltwater export's headline + snippet into a small classifiable
    body. The headline carries a lot of the topic signal, so we prepend it."""
    parts = [p.strip() for p in (headline, snippet) if p and p.strip()]
    return "\n\n".join(parts)


# Signals that a link is genuinely DEAD (the resource is gone or the host does
# not exist) — as opposed to merely blocked/paywalled. Per the client rule:
# dead -> "Not in scope"; blocked -> manual review.
#
# DEAD errors are limited to DNS-resolution failures (the domain does not
# resolve). Deliberately NOT included: "connection reset by peer", generic
# connect errors, or timeouts — those are often bot-walls/paywalls actively
# refusing us, which must go to manual review, never be dropped as Not in scope.
_DEAD_STATUS = {404, 410}
_DEAD_ERR_MARKERS = (
    "nameresolutionerror", "name or service not known", "getaddrinfo",
    "no address associated", "gaierror", "nodename nor servname",
    "failed to resolve", "could not resolve", "name not known",
)


def _reachability(fetched: dict) -> str:
    """Classify a fetch result as 'readable', 'dead', or 'blocked'.

      readable — got a real article body (classify it).
      dead     — 404/410, or the host could not be reached (DNS fail / refused).
                 Client rule: tag these "Not in scope".
      blocked  — reachable but unreadable: paywall/bot-wall (401/403/429), a
                 server error (5xx), a timeout, a JS-only/empty page, or only a
                 meta summary. Client rule: send these to manual review, NOT
                 "Not in scope" (they are often genuine, just unreadable).
    """
    if fetched.get("ok") and not fetched.get("summary_only"):
        return "readable"
    status = fetched.get("status")
    err = (fetched.get("error") or "").lower()
    if status in _DEAD_STATUS:
        return "dead"
    # No HTTP response at all + a host-unreachable error => dead link. (Timeouts
    # and other transient errors are deliberately treated as 'blocked', not dead,
    # so a slow-but-real article is never wrongly dropped as Not in scope.)
    if status is None and any(m in err for m in _DEAD_ERR_MARKERS):
        return "dead"
    return "blocked"


def classify_url(url: str, source: str = "", pub_country: str = "", byline: str = "",
                 snippet: str = "", headline: str = "", body: str = "",
                 prefer_snippet: bool = False) -> dict:
    """Classify one Bentley item.

    Text comes from the best source available, in this order of quality:
      1. `body`     — the export's full article text (best; no web fetch needed);
      2. a web fetch of the URL (when there is no body and prefer_snippet is off);
      3. `headline` + `snippet` — the export's short snippet (coarse; used as the
         fetch fallback, or directly when prefer_snippet is on).

    prefer_snippet=True classifies from the export text ONLY (body or snippet),
    never fetching — fast and reliable for a big export (skips paywall/JS/bot
    walls). prefer_snippet=False fetches the full article but falls back to the
    snippet instead of sending fetch failures to review.
    """
    result = {"url": url, "scope": None, "tags": [], "tags_by_family": {},
              "reason": "", "qa": "", "needs_review": [], "fetch": {},
              "live_rules_applied": 0, "text_source": None}

    # 1) deterministic block-list — no LLM, no fetch needed
    blocked = rules.blocked_source(url=url, source=source)
    if blocked:
        result.update(scope="out", reason=blocked,
                      tags=["Not in scope"], tags_by_family={"type_of_coverage": ["Not in scope"]})
        return result

    body = (body or "").strip()
    snippet_body = _snippet_text(headline, snippet)
    export_text = body or snippet_body          # best text the export row carries
    export_src = "export-body" if body else ("snippet" if snippet_body else "")

    def _skip_fetch(reason):
        result["fetch"] = {"ok": False, "chars": len(text), "status": None,
                           "error": None, "skipped": reason}

    # 2) resolve the text to classify.
    if prefer_snippet:
        #   export-text-only mode — never fetch.
        if not export_text:
            result.update(scope="review",
                          reason="Snippet-only mode, but the export row has no body or snippet text.",
                          needs_review=["no-export-text"])
            return result
        text = export_text
        result["text_source"] = export_src
        _skip_fetch("prefer_snippet")
    elif body:
        #   the export already carries the full article body — no fetch needed.
        text = body
        result["text_source"] = "export-body"
        _skip_fetch("export-body")
    else:
        #   open (fetch) the document URL and classify from its content. Then,
        #   per the client rule, branch on reachability:
        #     readable -> classify;  dead link -> Not in scope;  blocked -> review.
        fetched = fetch_article(url)
        result["fetch"] = {"ok": fetched["ok"], "chars": fetched["chars"],
                           "status": fetched["status"], "error": fetched["error"]}
        reach = _reachability(fetched)
        if reach == "readable":
            text = fetched["text"]
            result["text_source"] = "fetch"
            if fetched.get("author") and not byline:
                byline = fetched["author"]
        elif reach == "dead":
            # Dead / unreachable link (404/410 or host unreachable) -> Not in scope.
            detail = fetched.get("status") or (fetched.get("error") or "unreachable")
            result.update(scope="out", tags=["Not in scope"],
                          tags_by_family={"type_of_coverage": ["Not in scope"]},
                          reason=f"Dead / unreachable link ({detail}) — tagged Not in scope.",
                          text_source="dead-link")
            return result
        else:
            # Reachable but unreadable (paywall / bot-wall / JS-only / server
            # error / partial) -> manual review, NOT Not in scope. The export
            # snippet (if any) is kept on the result so a reviewer can use it.
            detail = fetched.get("status") or (fetched.get("error") or "unreadable")
            result.update(scope="review",
                          reason=f"Source reachable but not readable ({detail}) — likely a "
                                 "paywall/bot-wall or JS-only page. Flagged for manual review.",
                          needs_review=["manual-review-required: source unreadable (paywall/blocked)"],
                          text_source="blocked")
            if snippet_body:
                result["snippet_for_review"] = snippet_body
            return result

    # 3) ask Claude — base protocol prompt + any DB-stored client-feedback rules
    learned, n_learned = rules_block(taxonomy.RUN_BRAND)
    system_prompt = prompts.SYSTEM_PROMPT + ("\n\n" + learned if learned else "")
    result["live_rules_applied"] = n_learned

    # timeout so a slow/stuck call FLAGS for review instead of hanging for minutes
    client = Anthropic(timeout=90.0)
    try:
        resp = client.messages.create(
            model=config.MODEL,
            max_tokens=8000,
            # No extended thinking: it was the main time sink (~minutes on big/non-EN
            # articles). Classification is guided by explicit rules + deterministic
            # post-processing, so standard generation is plenty and far faster.
            system=system_prompt,
            messages=[{
                "role": "user",
                "content": prompts.ARTICLE_TEMPLATE.format(
                    source=source or _domain(url) or "(unknown outlet)",
                    pub_country=(pub_country or "(not provided - infer the region from the outlet's domain)"),
                    byline=(byline or "(none)"),
                    url=url,
                    text=text,
                ),
            }],
            output_config={"format": {"type": "json_schema", "schema": prompts.DECISION_SCHEMA}},
        )
    except Exception as e:
        emsg = str(e).lower()
        if "credit balance" in emsg or "billing" in emsg:
            # Systemic, not per-article: the Anthropic account is out of credits.
            result.update(scope="review",
                          reason="Anthropic API is out of credits — add credits in Plans & Billing "
                                 "and re-run. (This article was NOT classified.)",
                          needs_review=["anthropic-credits-exhausted"])
            return result
        result.update(scope="review",
                      reason=f"Classification did not complete ({type(e).__name__}: {e}) — timed out "
                             "or errored. Flagged for manual tagging.",
                      needs_review=[f"classification-error: {type(e).__name__}"])
        return result
    raw = next((b.text for b in resp.content if b.type == "text"), None)
    if raw is None:
        result.update(scope="review",
                      reason=f"no decision returned (stop_reason={resp.stop_reason}); "
                             "raise max_tokens or shorten text")
        return result
    d = json.loads(raw)

    result["reason"] = d.get("reasoning", "")
    result["qa"] = d.get("qa_validation", "")

    if d.get("decision") == "Not in Scope":
        result.update(scope="out", tags=["Not in scope"],
                      tags_by_family={"type_of_coverage": ["Not in scope"]})
        return result

    # 4) in scope -> assemble + enforce deterministic rules
    fam = _to_families(d)

    # de-duplicate every family (model sometimes repeats a tag)
    for k in list(fam):
        fam[k] = list(dict.fromkeys(fam[k]))

    # Product & Spokesperson are literal-name categories — scan the full text so
    # recall doesn't depend on the model. Add any taxonomy product named in the
    # text; validate spokespeople against the taxonomy (drops invented names like
    # "Greg Bentley") and add any taxonomy person named in the text.
    for lbl in taxonomy.products_in_text(text):
        if lbl not in fam.get("product", []):
            fam.setdefault("product", []).append(lbl)

    valid_sp = taxonomy.valid_spokesperson_labels()
    kept = [s for s in fam.get("spokesperson", []) if s in valid_sp]
    # Deterministic name-scan to boost recall — but require the name to appear
    # near "Bentley" so a same-name person at another company (e.g. "Brad Johnson"
    # appointed at Exacter) is NOT tagged as the Bentley spokesperson.
    for lbl in taxonomy.spokespeople_in_text(text, require_bentley_context=True):
        if lbl not in kept:
            kept.append(lbl)
    fam["spokesperson"] = kept

    # now enforce structural rules (product -> Corporate-Product&Technology, and
    # remove it when no product) AFTER the product scan
    fam = rules.enforce_structural_rules(fam)

    # Deterministic Coverage overrides (protocol), in priority order:
    #  1. Definitive Bentley-issued press-release boilerplate ("About Bentley
    #     Systems" tagline, "Nasdaq: BSY", "Bentley Systems today announced")
    #     => Press release, EVEN when a third-party site republished it with a
    #     byline. This is the strongest signal and wins over the byline rule.
    #  2. Otherwise a byline => Unique (a journalist wrote it), even if the piece
    #     reads like a wire/earnings release.
    byline_present = bool((byline or "").strip())
    cov = (fam.get("type_of_coverage") or [""])[0]
    if rules.is_bentley_press_release(text):
        fam["type_of_coverage"] = [rules.PRESS_RELEASE_LABEL]
    elif byline_present and ("Press release" in cov or "3rd party" in cov):
        fam["type_of_coverage"] = ["Type of Coverage - Unique"]
    result["scope"] = "in"
    result["tags_by_family"] = fam
    result["tags"] = _flatten(fam)

    # 5) flag for human review
    review = rules.missing_mandatory(fam)
    if result.get("text_source") in ("snippet", "snippet-fallback"):
        # classified from the Meltwater export snippet, not the full article —
        # tags are coarse, so a human should confirm.
        review.append("classified-from-snippet")
    # Spokespeople with no Meltwater tag can't be applied in Phase 2 — surface
    # them so a person can confirm / ask the client to create the tag.
    no_tag_sp = taxonomy.spokespeople_without_mw_tag()
    for sp in fam.get("spokesperson", []):
        if sp in no_tag_sp:
            review.append(f"spokesperson-no-meltwater-tag: {sp}")
    # tags the model itself was unsure about -> route to a human
    for u in (d.get("uncertain") or []):
        u = (u or "").strip()
        if u:
            review.append(f"uncertain: {u}")
    result["needs_review"] = review
    return result


def _print(r: dict) -> None:
    print("\n" + "=" * 70)
    print("URL   :", r["url"])
    print("SCOPE :", r["scope"], f"| live feedback rules applied: {r.get('live_rules_applied', 0)}")
    if r["fetch"]:
        f = r["fetch"]
        print("FETCH :", f"ok={f['ok']} chars={f['chars']} status={f['status']}"
              + (f" error={f['error']}" if f.get("error") else ""))
    print("REASON:", r["reason"])
    print("-" * 70)
    if r["tags"]:
        print("TAGS:")
        for fam, labels in r["tags_by_family"].items():
            for lbl in labels:
                print(f"   • {lbl}")
    if r["needs_review"]:
        print("⚠ REVIEW — missing mandatory:", ", ".join(r["needs_review"]))
    if r["qa"]:
        print("-" * 70)
        print("QA:", r["qa"])
    print("=" * 70)


def main():
    # Windows consoles default to cp1252 and crash on chars like "→" in the
    # model's QA text. Make stdout tolerant so printing never errors.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="Classify one Bentley article (Phase 1).")
    ap.add_argument("url")
    ap.add_argument("--source", default="")
    ap.add_argument("--country", default="")
    ap.add_argument("--byline", default="")
    ap.add_argument("--snippet", default="", help="Meltwater export snippet / hit sentence")
    ap.add_argument("--headline", default="", help="Meltwater export headline / title")
    ap.add_argument("--body", default="", help="Meltwater export full body text")
    ap.add_argument("--snippet-only", action="store_true",
                    help="classify from the export text (body/snippet) only, no web fetch")
    ap.add_argument("--json", action="store_true", help="print raw JSON result")
    args = ap.parse_args()

    r = classify_url(args.url, source=args.source, pub_country=args.country, byline=args.byline,
                     snippet=args.snippet, headline=args.headline, body=args.body,
                     prefer_snippet=args.snippet_only)
    if args.json:
        print(json.dumps(r, indent=2, ensure_ascii=False))
    else:
        _print(r)


if __name__ == "__main__":
    main()
