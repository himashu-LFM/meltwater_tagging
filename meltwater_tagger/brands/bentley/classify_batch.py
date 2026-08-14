"""
Bentley batch classifier.

Runs the single-article Bentley classifier over MANY URLs concurrently and
writes a decisions.json. Input can be:
  • a text file with one URL per line (blank lines / '#' comments ignored), or
  • URLs passed directly as arguments.

When a real Meltwater export is available we'll add a mode that reads the .xlsx
and passes the per-row metadata (publication country, byline, snippet text) to
each classification — for now, bare URLs are fine for accuracy testing.

CLI:
    python -m brands.bentley.classify_batch urls.txt
    python -m brands.bentley.classify_batch https://a.com/x https://b.com/y
    python -m brands.bentley.classify_batch urls.txt --out decisions_bentley.json --workers 6
"""

import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

from brands.bentley.classify_bentley import classify_url

RUN_BRAND = "Bentley"


def _read_urls(args_urls: list[str]) -> list[str]:
    urls: list[str] = []
    for item in args_urls:
        # a .txt file of URLs, or a literal URL
        if item.lower().endswith(".txt"):
            with open(item, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        urls.append(line)
        else:
            urls.append(item.strip())
    # de-dupe, keep order
    seen, out = set(), []
    for u in urls:
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out


def run(urls: list[str], workers: int = 6) -> list[dict]:
    results: list[dict] = []
    total = len(urls)
    print(f"Classifying {total} URL(s) with {workers} workers…\n")

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(classify_url, u): u for u in urls}
        for i, fut in enumerate(as_completed(futures), 1):
            u = futures[fut]
            try:
                r = fut.result()
            except Exception as e:
                r = {"url": u, "scope": "error", "reason": f"{type(e).__name__}: {e}",
                     "tags": [], "needs_review": ["classification-crashed"]}
            results.append(r)
            scope = (r.get("scope") or "?").upper()
            ntags = len(r.get("tags", []))
            flag = " ⚠" if r.get("needs_review") else ""
            print(f"[{i}/{total}] {scope:6s} {ntags:2d} tags{flag}  {u[:80]}")

    # keep input order in the output
    order = {u: i for i, u in enumerate(urls)}
    results.sort(key=lambda r: order.get(r["url"], 1e9))
    return results


def _summary(results: list[dict]) -> None:
    inn = sum(1 for r in results if r.get("scope") == "in")
    out = sum(1 for r in results if r.get("scope") == "out")
    rev = sum(1 for r in results if r.get("needs_review"))
    err = sum(1 for r in results if r.get("scope") in ("error", "review"))
    print("\n" + "=" * 60)
    print(f"TOTAL {len(results)}  |  in-scope {inn}  |  not-in-scope {out}  "
          f"|  needs-review {rev}  |  errors/unresolved {err}")
    print("=" * 60)


def _save_to_db(results: list[dict], user_id: str) -> None:
    """Save the run to the shared tagging_runs table (same store the webapp uses).

    Opt-in only. Needs a Supabase user_id (which analyst's history to attach to)
    because every run is scoped to a user. Fails soft — a DB problem never loses
    the local decisions.json.

    NOTE: tagging_runs.results is flexible JSONB, so Bentley's {url, scope, tags}
    rows store fine. The History PAGE still renders Kaseya's sentiment shape, so
    Bentley rows won't display nicely there yet — that's a later dashboard change.
    """
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "webapp"))
    try:
        import db
    except Exception as e:
        print(f"⚠ DB save skipped — could not import db module: {e}")
        return
    if not db.is_configured():
        print("⚠ DB save skipped — Supabase not configured (.env).")
        return
    if not user_id:
        print("⚠ DB save skipped — no user id. Pass --user-id <id> or set "
              "MELTWATER_USER_ID (your Supabase Authentication → Users id).")
        return
    try:
        row = db.save_run(user_id, RUN_BRAND, results, status="classified")
        print(f"✓ Saved to DB (tagging_runs) — run id: {row.get('id', '?')}")
    except Exception as e:
        print(f"⚠ DB save failed (local JSON is safe): {e}")


def main():
    ap = argparse.ArgumentParser(description="Classify many Bentley URLs → decisions.json")
    ap.add_argument("urls", nargs="+", help="URLs and/or a .txt file of URLs")
    ap.add_argument("--out", default="decisions_bentley.json")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--save-db", action="store_true",
                    help="also save the run to the shared tagging_runs table")
    ap.add_argument("--user-id", default=os.environ.get("MELTWATER_USER_ID", ""),
                    help="Supabase user id to attach the run to (or set MELTWATER_USER_ID)")
    args = ap.parse_args()

    urls = _read_urls(args.urls)
    if not urls:
        print("No URLs found.", file=sys.stderr)
        sys.exit(1)

    results = run(urls, workers=args.workers)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)

    _summary(results)
    print(f"\nWrote {args.out}")

    if args.save_db:
        _save_to_db(results, args.user_id)


if __name__ == "__main__":
    main()
