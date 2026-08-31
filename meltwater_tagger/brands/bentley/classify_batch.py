"""
Bentley batch classifier.

Runs the single-article Bentley classifier over MANY items concurrently and
writes a decisions.json. Input can be:
  • a Meltwater export (.xlsx / .csv) — the REAL production input: each row
    carries the URL plus publication country, byline, headline and snippet, so
    classification uses that metadata directly (and can skip web fetching);
  • a text file with one URL per line (blank lines / '#' comments ignored); or
  • URLs passed directly as arguments.

CLI:
    python -m brands.bentley.classify_batch export.xlsx
    python -m brands.bentley.classify_batch export.xlsx --snippet-only   # no web fetch
    python -m brands.bentley.classify_batch urls.txt
    python -m brands.bentley.classify_batch https://a.com/x https://b.com/y
    python -m brands.bentley.classify_batch export.xlsx --out decisions_bentley.json --workers 6
"""

import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

from brands.bentley.classify_bentley import classify_url
from brands.bentley.export_reader import read_export

RUN_BRAND = "Bentley"

_EXPORT_EXTS = (".xlsx", ".xls", ".csv")


def _build_jobs(args_inputs: list[str]) -> list[dict]:
    """Turn CLI inputs into classification jobs (dicts with a url + any export
    metadata). Accepts a Meltwater export (.xlsx/.csv), a .txt of URLs, and/or
    literal URLs, mixed freely. De-duped by URL, input order preserved."""
    jobs: list[dict] = []
    for item in args_inputs:
        low = item.lower()
        if low.endswith(_EXPORT_EXTS):
            rows, cols = read_export(item)
            detected = {k: v for k, v in cols.items() if v}
            print(f"Loaded {len(rows)} row(s) from {item}\n  detected columns: {detected}\n")
            jobs.extend(rows)
        elif low.endswith(".txt"):
            with open(item, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        jobs.append({"url": line})
        else:
            jobs.append({"url": item.strip()})
    # de-dupe by url, keep order
    seen, out = set(), []
    for j in jobs:
        u = j.get("url", "")
        if u and u not in seen:
            seen.add(u)
            out.append(j)
    return out


def _classify_job(job: dict, prefer_snippet: bool) -> dict:
    r = classify_url(
        job["url"],
        source=job.get("source", ""),
        pub_country=job.get("pub_country", ""),
        byline=job.get("byline", ""),
        snippet=job.get("snippet", ""),
        headline=job.get("headline", ""),
        body=job.get("body", ""),
        prefer_snippet=prefer_snippet,
    )
    # Carry apply-critical export metadata onto the result so decisions.json is
    # ready for Phase-2 apply: `document_id` is what Meltwater's tag API targets,
    # and `document_tags` are the tags ALREADY applied (additive-skip + a
    # ground-truth reference for accuracy checks).
    for k in ("document_id", "document_tags", "source", "pub_country", "byline",
              "headline", "date", "snippet", "keywords"):
        if job.get(k):
            r.setdefault(k, job[k])
    return r


def run(jobs: list, workers: int = 6, prefer_snippet: bool = False) -> list[dict]:
    # Accept a list of URL strings (the dashboard passes these) OR job dicts
    # (the export path). Normalize bare strings to {"url": ...}.
    jobs = [{"url": j} if isinstance(j, str) else j for j in jobs]
    results: list[dict] = []
    total = len(jobs)
    mode = "snippet-only (no fetch)" if prefer_snippet else "fetch + snippet-fallback"
    print(f"Classifying {total} item(s) with {workers} workers [{mode}]...\n")

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(_classify_job, job, prefer_snippet): job for job in jobs}
        for i, fut in enumerate(as_completed(futures), 1):
            job = futures[fut]
            u = job.get("url", "")
            try:
                r = fut.result()
            except Exception as e:
                r = {"url": u, "scope": "error", "reason": f"{type(e).__name__}: {e}",
                     "tags": [], "needs_review": ["classification-crashed"]}
            results.append(r)
            scope = (r.get("scope") or "?").upper()
            ntags = len(r.get("tags", []))
            src = (r.get("text_source") or "?")
            # distinguish "held for a human to read" from "tagged, just confirm"
            if (r.get("scope") or "") in ("review", "error"):
                flag = " [needs-read]"
            elif r.get("needs_review"):
                flag = " [confirm]"
            else:
                flag = ""
            print(f"[{i}/{total}] {scope:6s} {ntags:2d} tags via {src:16s}{flag}  {u[:70]}")

    # keep input order in the output
    order = {j["url"]: i for i, j in enumerate(jobs)}
    results.sort(key=lambda r: order.get(r["url"], 1e9))
    return results


def _summary(results: list[dict]) -> None:
    inn = sum(1 for r in results if r.get("scope") == "in")
    out = sum(1 for r in results if r.get("scope") == "out")
    held = sum(1 for r in results if r.get("scope") == "review")   # couldn't read -> a human must read it
    err = sum(1 for r in results if r.get("scope") == "error")
    # in-scope items that ARE tagged but carry a soft "confirm" flag (uncertain / snippet-based)
    confirm = sum(1 for r in results if r.get("scope") == "in" and r.get("needs_review"))
    tagged = inn + out
    print("\n" + "=" * 66)
    print(f"TOTAL {len(results)}")
    print(f"  TAGGED automatically: {tagged}   (in-scope {inn} | not-in-scope {out})")
    print(f"     ...of which flagged to CONFIRM (already tagged, optional check): {confirm}")
    print(f"  NEEDS A HUMAN to read (blocked/unreadable, NOT tagged): {held}")
    if err:
        print(f"  errors: {err}")
    print("=" * 66)


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
    ap = argparse.ArgumentParser(
        description="Classify a Meltwater export / URLs → decisions.json")
    ap.add_argument("inputs", nargs="+",
                    help="a Meltwater export (.xlsx/.csv), a .txt of URLs, and/or literal URLs")
    ap.add_argument("--out", default="decisions_bentley.json")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--snippet-only", action="store_true",
                    help="classify from the export snippet/headline only — no web fetch "
                         "(fast + avoids paywall/JS/bot walls; recommended for big exports)")
    ap.add_argument("--save-db", action="store_true",
                    help="also save the run to the shared tagging_runs table")
    ap.add_argument("--user-id", default=os.environ.get("MELTWATER_USER_ID", ""),
                    help="Supabase user id to attach the run to (or set MELTWATER_USER_ID)")
    args = ap.parse_args()

    try:
        jobs = _build_jobs(args.inputs)
    except (FileNotFoundError, ValueError) as e:
        print(f"Could not read input: {e}", file=sys.stderr)
        sys.exit(1)
    if not jobs:
        print("No URLs found.", file=sys.stderr)
        sys.exit(1)

    results = run(jobs, workers=args.workers, prefer_snippet=args.snippet_only)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)

    _summary(results)
    print(f"\nWrote {args.out}")

    if args.save_db:
        _save_to_db(results, args.user_id)


if __name__ == "__main__":
    main()
