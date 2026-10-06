"""
Per-family accuracy scorer for Bentley tagging.

Compares the tool's tags against a human "correct" column, family by family, so
we can see EXACTLY where the classifier lags instead of eyeballing. Works on the
"Bentley automation result*.xlsx" review files (columns: a URL, our tags, the
human's correct tags) and on any future re-run exported the same way.

Both columns are parsed with the SAME tolerant matcher: our output is canonical
("Corporate - M&A", "Industry | AEC"), the human column is free-form
("Corporate: Corporate – M&A; Corporate – Product & Technology"). A
specificity-ordered, span-consuming matcher maps each recognised tag value to a
(family, canonical-value) pair and blanks the matched span, so overlaps like
"Technology" (publication) vs "Product & Technology" (corporate) don't
double-count.

Per family it reports: rows compared, exact-set agreement %, and every mismatch
(url, ours, human) so you can read the actual disagreements. Scope (in vs
not-in-scope) is scored first because it dominates everything else.

Usage:
    python -m brands.bentley.score_accuracy "Bentley automation result.xlsx"
    python -m brands.bentley.score_accuracy file.xlsx --ours "Sentiment Tagger tags" --human "Correct tags"
"""

import argparse
import re

import pandas as pd

from brands.bentley import taxonomy as tax

# CONTENT families drive the in-scope tag comparison and the scope decision.
# "status" (e.g. Inaccessible) is NOT content — it marks a dead/non-existent URL
# — so it is kept out of the in-scope loop and scored on its own over all rows.
CONTENT_FAMILIES = ["publication", "coverage", "region", "corporate", "pillar",
                    "industry", "product", "spokesperson"]
FAMILIES = CONTENT_FAMILIES + ["status"]   # all keys parse_tags populates

# Ordered MOST-SPECIFIC FIRST. Each match records (family, canonical) and its text
# span is consumed so a later, shorter pattern can't re-match inside it.
_PATTERNS = [
    # --- status (distinctive single word; match before anything else) ---
    (r"\binaccessible\b", "status", "inaccessible"),
    # --- coverage ---
    (r"3rd\s*part(?:y|ies)\s*press\s*release", "coverage", "3rd party press release"),
    (r"third\s*part(?:y|ies)\s*press\s*release", "coverage", "3rd party press release"),
    (r"not\s*in\s*scope", "coverage", "not in scope"),
    (r"press\s*release", "coverage", "press release"),
    (r"\bunique\b", "coverage", "unique"),
    # --- corporate (product & technology BEFORE publication 'technology') ---
    (r"product\s*(?:&|and)\s*technology", "corporate", "product & technology"),
    (r"m\s*&\s*a|mergers?\s*(?:&|and)\s*acquisitions?", "corporate", "m&a"),
    (r"financial\s*/?\s*ir|financial|investor\s*relations|\bir\b", "corporate", "financial/ir"),
    (r"hr\s*/?\s*colleague|colleague\s*success|\bhr\b|human\s*resources", "corporate", "hr/colleague success"),
    (r"events?\s*/?\s*milestones?\s*/?\s*awards?|milestones?|\bawards?\b|\bevents?\b", "corporate", "events/milestones/awards"),
    (r"csr|compliance|donation", "corporate", "csr/compliance/donation"),
    (r"government\s*/?\s*policy|government|policy", "corporate", "government/policy"),
    (r"sustainab\w*", "corporate", "sustainability"),
    (r"education|stem", "corporate", "education"),
    (r"corporate\s*[–\-:]?\s*general|\bgeneral\b", "corporate", "general"),
    # --- pillar ---
    (r"infrastructure\s*ai|\bai\b|artificial\s*intelligence", "pillar", "infrastructure ai"),
    (r"connected\s*data", "pillar", "connected data"),
    (r"resil\w*\s*built\s*world|resil\w*", "pillar", "resilient built world"),
    # --- products (before publication 'technology'; distinctive names) ---
    (r"bentley\s*infrastructure\s*cloud", "product", "bentley infrastructure cloud"),
    (r"openutilities\s*substation\+?", "product", "openutilities substation+"),
    (r"opensite\+?", "product", "opensite+"),
    (r"microstation", "product", "microstation"),
    (r"projectwise", "product", "projectwise"),
    (r"assetwise", "product", "assetwise"),
    (r"synchro\+", "product", "synchro+"),
    (r"synchro", "product", "synchro"),
    (r"blyncsy", "product", "blyncsy"),
    (r"\bstaad\b", "product", "staad"),
    (r"\bitwin\b", "product", "itwin"),
    # --- industry ---
    (r"electric\s*utilities|transmission\s*(?:&|and)?\s*distribution", "industry", "energy|electric utilities"),
    (r"power\s*generation", "industry", "energy|power generation"),
    (r"rail\s*(?:&|and)?\s*transit|\brail\b|\btransit\b", "industry", "transportation|rail & transit"),
    (r"airports?\s*(?:&|and)?\s*ports?|\bairport\b", "industry", "transportation|airports & ports"),
    (r"roads?\s*(?:&|and)?\s*highways?|\bhighway\b", "industry", "transportation|roads & highways"),
    (r"bridges?\s*(?:&|and)?\s*tunnels?|\bbridge\b|\btunnel\b", "industry", "transportation|bridges & tunnels"),
    (r"\bmining\b", "industry", "mining"),
    (r"\bwater\b", "industry", "water"),
    (r"\bcities\b|\bcity\b", "industry", "cities"),
    (r"\baec\b", "industry", "aec"),
    # --- publication (technology AFTER product&tech + product names consumed) ---
    (r"mainstream\s*/?\s*business|mainstream|\bbusiness\b", "publication", "mainstream/business"),
    (r"trade\s*/?\s*industry|\btrade\b", "publication", "trade/industry"),
    (r"\btechnology\b|\btech\b", "publication", "technology"),
    # --- region ---
    (r"\bnala\b", "region", "nala"),
    (r"\bemea\b", "region", "emea"),
    (r"\bapac\b", "region", "apac"),
]
_COMPILED = [(re.compile(p, re.I), fam, val) for p, fam, val in _PATTERNS]

# Spokesperson names (+aliases) from the taxonomy, longest first so a full name
# is consumed before a partial.
_SP_NAMES = sorted(
    {n for sp in tax.SPOKESPEOPLE for n in ([sp["name"]] + sp.get("aliases", []))},
    key=len, reverse=True,
)


def _norm(cell) -> str:
    s = "" if cell is None else str(cell)
    s = s.replace("–", "-").replace("—", "-").replace("|", " ")
    return re.sub(r"\s+", " ", s).strip()


def parse_tags(cell) -> dict:
    """Return {family: set(canonical values)} found in a free-form or canonical
    tag cell, via specificity-ordered span-consuming matching."""
    text = _norm(cell)
    out = {f: set() for f in FAMILIES}
    if not text:
        return out
    # Spokespeople first (names are distinctive; consume them so 'James Lee' etc.
    # can't be mis-parsed by other patterns).
    low = text.lower()
    consumed = [False] * len(text)
    for name in _SP_NAMES:
        for m in re.finditer(r"(?<!\w)" + re.escape(name.lower()) + r"(?!\w)", low):
            out["spokesperson"].add(tax.spokesperson_label(_resolve_sp(name)))
            for i in range(m.start(), m.end()):
                consumed[i] = True
    # blank consumed spans
    chars = [(" " if consumed[i] else c) for i, c in enumerate(text)]
    text = "".join(chars)
    for rx, fam, val in _COMPILED:
        for m in list(rx.finditer(text)):
            out[fam].add(val)
        text = rx.sub(lambda mo: " " * (mo.end() - mo.start()), text)
    return out


def _resolve_sp(name: str) -> str:
    """Map an alias to its canonical spokesperson name."""
    nl = name.lower()
    for sp in tax.SPOKESPEOPLE:
        if sp["name"].lower() == nl or nl in [a.lower() for a in sp.get("aliases", [])]:
            return sp["name"]
    return name


def _scope(parsed: dict) -> str:
    cov = parsed["coverage"]
    # A status tag (Inaccessible) or the explicit "Not in scope" marker -> out.
    # status is deliberately excluded from the "any content?" test below so an
    # Inaccessible-only row is not mistaken for in-scope coverage.
    if parsed.get("status") or cov == {"not in scope"}:
        return "out"
    if not any(parsed[f] for f in CONTENT_FAMILIES):
        return "out"          # empty cell / pure note -> treat as not-in-scope
    return "in"


def score(df: pd.DataFrame, ours_col: str, human_col: str, url_col: str) -> None:
    rows = []
    for _, r in df.iterrows():
        u = str(r.get(url_col, "")).strip()
        if not u or u.lower() == "nan":
            continue
        rows.append((u, parse_tags(r.get(ours_col)), parse_tags(r.get(human_col))))
    n = len(rows)
    print(f"\nScored {n} rows  (ours='{ours_col}'  human='{human_col}')\n" + "=" * 72)

    # scope
    sc_ok = sum(1 for _, o, h in rows if _scope(o) == _scope(h))
    print(f"SCOPE (in vs not-in-scope) agreement: {sc_ok}/{n} = {sc_ok/n*100:.0f}%")
    scope_miss = [(u, _scope(o), _scope(h)) for u, o, h in rows if _scope(o) != _scope(h)]
    for u, o, h in scope_miss:
        print(f"    ~ ours={o:3}  human={h:3}  {u[:80]}")

    # per family, only over rows both consider in-scope (tag families are
    # meaningless for a not-in-scope item)
    both_in = [(u, o, h) for u, o, h in rows if _scope(o) == "in" and _scope(h) == "in"]
    print(f"\nPer-family agreement over the {len(both_in)} rows both call IN-SCOPE:")
    print("-" * 72)
    for fam in CONTENT_FAMILIES:
        comp = [(u, o[fam], h[fam]) for u, o, h in both_in if o[fam] or h[fam]]
        if not comp:
            print(f"{fam:13}  (no values on either side)")
            continue
        ok = sum(1 for _, a, b in comp if a == b)
        print(f"{fam:13}  {ok}/{len(comp)} = {ok/len(comp)*100:3.0f}%   "
              f"(rows with a value here)")
        for u, a, b in comp:
            if a != b:
                miss = ", ".join(sorted(b - a)) or "∅"
                extra = ", ".join(sorted(a - b)) or "∅"
                print(f"      ~ missing:[{miss}]  extra:[{extra}]  {u[:60]}")

    # status (e.g. Inaccessible) — scored over ALL rows, independent of scope,
    # since it marks out-of-scope (dead/non-existent) URLs that the per-family
    # in-scope loop above deliberately excludes.
    st_comp = [(u, o["status"], h["status"]) for u, o, h in rows if o["status"] or h["status"]]
    if st_comp:
        ok = sum(1 for _, a, b in st_comp if a == b)
        print(f"\nSTATUS (Inaccessible) agreement over the {len(st_comp)} rows "
              f"with a status tag: {ok}/{len(st_comp)} = {ok/len(st_comp)*100:.0f}%")
        for u, a, b in st_comp:
            if a != b:
                print(f"      ~ ours:[{', '.join(sorted(a)) or '∅'}]  "
                      f"human:[{', '.join(sorted(b)) or '∅'}]  {u[:60]}")


def main():
    ap = argparse.ArgumentParser(description="Per-family accuracy scorer (Bentley).")
    ap.add_argument("file")
    ap.add_argument("--ours", default="Sentiment Tagger tags")
    ap.add_argument("--human", default="Correct tags")
    ap.add_argument("--url", default="")
    ap.add_argument("--sheet", default=None, help="sheet name (default: every sheet)")
    args = ap.parse_args()

    xl = pd.ExcelFile(args.file)
    sheets = [args.sheet] if args.sheet else xl.sheet_names
    for sh in sheets:
        df = xl.parse(sh)
        df.columns = [str(c).strip() for c in df.columns]
        if args.ours not in df.columns or args.human not in df.columns:
            continue
        url_col = args.url or next((c for c in df.columns if "url" in c.lower()), df.columns[0])
        print(f"\n########## sheet: {sh} ##########")
        score(df, args.ours, args.human, url_col)


if __name__ == "__main__":
    main()
