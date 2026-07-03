"""
Accuracy check: compare our tags against a manual-tagged column.

Point it at an .xlsx that has BOTH our tag column and the senior's manual column.
Prints overall agreement, a confusion matrix, and every mismatch — so you can see
exactly where (and why) the model and the human differ, and tune the prompt.

Usage:
    python compare.py "file.xlsx" [--ours tag_to_apply] [--manual "Manual tag"]
"""

import argparse
import collections

import pandas as pd


def sentiment(x) -> str:
    x = str(x).lower()
    for s in ("positive", "negative", "neutral"):
        if s in x:
            return s
    return "none"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("file")
    ap.add_argument("--ours", default="tag_to_apply")
    ap.add_argument("--manual", default="Manual tag")
    args = ap.parse_args()

    df = pd.read_excel(args.file)
    if args.ours not in df.columns or args.manual not in df.columns:
        raise SystemExit(f"Need columns {args.ours!r} and {args.manual!r}. Found: {list(df.columns)}")

    m = df[df[args.manual].notna() & (df[args.manual].astype(str).str.strip() != "")].copy()
    m["ours"] = m[args.ours].map(sentiment)
    m["man"] = m[args.manual].map(sentiment)

    agree = (m["ours"] == m["man"]).sum()
    n = len(m)
    print(f"Manually-tagged rows: {n}")
    print(f"Agreement: {agree}/{n} ({agree/n*100:.0f}%)\n")

    print("Confusion (ours -> manual):")
    for (o, mm), c in collections.Counter(zip(m["ours"], m["man"])).most_common():
        flag = "  OK" if o == mm else "  X"
        print(f"  ours={o:9} manual={mm:9} : {c}{flag}")

    print("\nMismatches:")
    for _, r in m[m["ours"] != m["man"]].iterrows():
        reason = str(r.get("reason", ""))[:90]
        print(f"  ours={r['ours']:8} manual={r['man']:8} | {reason}")
        print(f"      {r.get('permalink','')}")


if __name__ == "__main__":
    main()
