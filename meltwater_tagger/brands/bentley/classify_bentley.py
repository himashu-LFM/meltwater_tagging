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

from anthropic import Anthropic

import config
from brands.bentley import prompts, rules, taxonomy
from brands.bentley.fetcher import fetch_article
from brands.bentley.live_rules import rules_block

_SINGLE = ["type_of_publication", "type_of_coverage", "region"]
_MULTI = ["corporate", "pillar", "industry", "product", "spokesperson"]


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


def classify_url(url: str, source: str = "", pub_country: str = "", byline: str = "") -> dict:
    result = {"url": url, "scope": None, "tags": [], "tags_by_family": {},
              "reason": "", "qa": "", "needs_review": [], "fetch": {},
              "live_rules_applied": 0}

    # 1) deterministic block-list — no LLM, no fetch needed
    blocked = rules.blocked_source(url=url, source=source)
    if blocked:
        result.update(scope="out", reason=blocked,
                      tags=["Not in scope"], tags_by_family={"type_of_coverage": ["Not in scope"]})
        return result

    # 2) fetch article text
    fetched = fetch_article(url)
    result["fetch"] = {"ok": fetched["ok"], "chars": fetched["chars"],
                       "status": fetched["status"], "error": fetched["error"]}
    text = fetched["text"] or "(article text could not be fetched)"

    # 3) ask Claude — base protocol prompt + any DB-stored client-feedback rules
    learned, n_learned = rules_block(taxonomy.RUN_BRAND)
    system_prompt = prompts.SYSTEM_PROMPT + ("\n\n" + learned if learned else "")
    result["live_rules_applied"] = n_learned

    client = Anthropic()  # reads ANTHROPIC_API_KEY from env (config loaded it)
    resp = client.messages.create(
        model=config.MODEL,
        max_tokens=6000,
        thinking={"type": "adaptive"},
        system=system_prompt,
        messages=[{
            "role": "user",
            "content": prompts.ARTICLE_TEMPLATE.format(
                source=source or "(unknown)",
                pub_country=pub_country or "(unknown)",
                byline=byline or "(none)",
                url=url,
                text=text,
            ),
        }],
        output_config={"format": {"type": "json_schema", "schema": prompts.DECISION_SCHEMA}},
    )
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
    fam = rules.enforce_structural_rules(fam)      # product -> Corporate-Product&Tech
    result["scope"] = "in"
    result["tags_by_family"] = fam
    result["tags"] = _flatten(fam)

    # 5) flag for human review
    review = rules.missing_mandatory(fam)
    if not fetched["ok"]:
        # classified on metadata/URL only — tags are a guess, don't trust silently
        review.append("article-text-not-fetched")
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
    ap = argparse.ArgumentParser(description="Classify one Bentley article (Phase 1).")
    ap.add_argument("url")
    ap.add_argument("--source", default="")
    ap.add_argument("--country", default="")
    ap.add_argument("--byline", default="")
    ap.add_argument("--json", action="store_true", help="print raw JSON result")
    args = ap.parse_args()

    r = classify_url(args.url, source=args.source, pub_country=args.country, byline=args.byline)
    if args.json:
        print(json.dumps(r, indent=2, ensure_ascii=False))
    else:
        _print(r)


if __name__ == "__main__":
    main()
