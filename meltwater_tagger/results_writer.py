"""Write run results to a timestamped Excel file under results/."""

import os
from datetime import datetime

import pandas as pd

RESULTS_DIR = os.environ.get(
    "MELTWATER_RESULTS_DIR", os.path.join(os.path.dirname(__file__), "results")
)


def write_results_excel(rows: list[dict], kind: str, brand: str) -> str:
    """
    Write rows to results/<kind>_<brand>_<timestamp>.xlsx and return the path.

    Filename uses YYYYMMDD_HHMMSS so the newest run sorts last by name and is
    newest by file date — sort the folder by 'Date modified' (descending) to see
    the most recent run first.
    """
    os.makedirs(RESULTS_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_brand = (brand or "run").replace(" ", "_")
    path = os.path.join(RESULTS_DIR, f"{kind}_{safe_brand}_{ts}.xlsx")

    df = pd.DataFrame(rows)
    df.to_excel(path, index=False)
    return path
