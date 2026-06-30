"""
Convert an existing decisions.json into a classification Excel under results/.
Use this to review prior classifications without re-running (no API credits used).

Usage:
    python export_decisions.py [decisions.json]
"""

import json
import sys

import config
from results_writer import write_results_excel

path = sys.argv[1] if len(sys.argv) > 1 else config.DECISIONS_FILE
data = json.load(open(path, encoding="utf-8"))
rows = [{
    "action": d.get("action"),
    "tag_to_apply": d.get("tag") or "",
    "flag_brand": d.get("flag_brand", ""),
    "reason": d.get("reason", ""),
    "permalink": d.get("permalink"),
} for d in data["decisions"]]
out = write_results_excel(rows, kind="classification", brand=data.get("run_brand", "run"))
n = sum(1 for d in data["decisions"] if d.get("tag"))
print(f"{len(rows)} rows ({n} tags to apply) -> {out}")
