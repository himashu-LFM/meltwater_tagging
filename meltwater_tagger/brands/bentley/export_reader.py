"""
Read a Meltwater export (.xlsx / .csv) into normalized rows for classification.

The Meltwater export is the REAL production input. Unlike bare-URL testing, each
row already carries the metadata Region and Type-of-Coverage depend on
(publication country, author byline) plus the article text or a snippet — so we
can classify without fetching every URL (which trips paywalls / JS / bot walls).

Two robustness details learned from real exports:
  * The text lives in whichever of several columns is populated — a given export
    may fill `Body` (full text), or only `Opening Text`, or only `Hit Sentence`.
    So each field is read from an ORDERED list of candidate columns, taking the
    first non-empty value PER ROW (not per file).
  * `Document ID` and `Document Tags` are captured too: Phase-2 apply targets a
    document id, and the already-applied Document Tags let apply skip tags that
    are already present (additive tagging) — and serve as a ground-truth
    reference when measuring classification accuracy.

Each returned row is a dict:
    {url, source, source_domain, pub_country, byline, snippet, body, headline,
     date, document_id, document_tags}
Missing values are empty strings (never NaN).
"""

import os

import pandas as pd


# Ordered candidate headers per field (case-insensitive; exact match wins over
# substring). Order matters: the first column that has a value for a given row
# is used, so put the richest/preferred source first.
FIELD_HINTS = {
    "url":           ["url", "permalink", "link", "article url", "hit url"],
    "headline":      ["headline", "title", "article title"],
    "body":          ["body", "full text", "article text", "article body"],
    # snippet: Opening Text is fuller than the one-line Hit Sentence — prefer it,
    # but many exports fill only one of them. (No bare "content" hint — it would
    # substring-match the unrelated "Content Type" column.)
    "snippet":       ["opening text", "hit sentence", "snippet", "summary"],
    # deliberately NOT a bare "source" hint — that would grab Source Domain/Type.
    "source":        ["source name", "media outlet", "outlet", "publication name", "publisher"],
    "source_domain": ["source domain", "domain"],
    "pub_country":   ["publication country", "source country", "country of origin", "country"],
    "byline":        ["author name", "author", "byline", "journalist", "writer"],
    "date":          ["date", "published date", "publish date", "publication date", "published"],
    "document_id":   ["document id", "doc id", "mention id"],
    # No bare "tags" hint — it would substring-match "Hashtags".
    "document_tags": ["document tags", "existing tags"],
    # Meltwater's tagging API echoes back the matched keywords; capture them.
    "keywords":      ["keywords", "keyphrases"],
}

# Resolve in this order so a column claimed by an earlier field can't be reused
# (e.g. Source Domain is taken before Source, URL before everything).
_RESOLVE_ORDER = ["url", "document_id", "source_domain", "headline", "body",
                  "snippet", "source", "pub_country", "byline", "date",
                  "document_tags", "keywords"]


def _matching_cols(df: pd.DataFrame, hints: list[str], taken: set[str]) -> list[str]:
    """All original column names matching any hint, in hint-priority order,
    skipping columns already claimed by an earlier field. Exact (case-
    insensitive) matches for a hint come before substring matches for it."""
    avail = {c.lower().strip(): c for c in df.columns if c not in taken}
    out: list[str] = []
    for h in hints:
        for low, orig in avail.items():
            if low == h and orig not in out:
                out.append(orig)
        for low, orig in avail.items():
            if h in low and orig not in out:
                out.append(orig)
    return out


def _detect_columns(df: pd.DataFrame) -> dict:
    """Map each field -> ordered list of the export's actual column names."""
    cols: dict = {}
    taken: set[str] = set()
    for field in _RESOLVE_ORDER:
        matches = _matching_cols(df, FIELD_HINTS[field], taken)
        cols[field] = matches
        taken.update(matches)
    return cols


def _clean(v) -> str:
    """A cell value as clean text: NaN/None -> '', trimmed string otherwise."""
    if v is None:
        return ""
    try:
        if pd.isna(v):
            return ""
    except (TypeError, ValueError):
        pass
    return str(v).strip()


def _first(row, colnames: list[str]) -> str:
    """First non-empty cleaned value across the candidate columns for this row."""
    for c in colnames:
        val = _clean(row.get(c))
        if val:
            return val
    return ""


def rows_from_df(df) -> tuple[list[dict], dict]:
    """Turn an already-parsed export DataFrame into (rows, detected_columns).

    Split out from read_export so callers that already have a DataFrame (the web
    app reads the upload straight into pandas) can reuse the same column
    detection + row normalization. Raises ValueError if no URL column exists."""
    cols = _detect_columns(df)
    if not cols["url"]:
        raise ValueError(
            "Could not find a URL column in the export. Columns present: "
            f"{list(df.columns)}. Add/rename a column so its header contains "
            "'url' or 'link', or extend export_reader.FIELD_HINTS['url']."
        )
    rows: list[dict] = []
    seen: set[str] = set()
    for _, r in df.iterrows():
        url = _first(r, cols["url"])
        if not url or url in seen:
            continue                      # skip blanks + duplicate URLs
        seen.add(url)
        rows.append({
            "url":           url,
            "source":        _first(r, cols["source"]),
            "source_domain": _first(r, cols["source_domain"]),
            "pub_country":   _first(r, cols["pub_country"]),
            "byline":        _first(r, cols["byline"]),
            "snippet":       _first(r, cols["snippet"]),
            "body":          _first(r, cols["body"]),
            "headline":      _first(r, cols["headline"]),
            "date":          _first(r, cols["date"]),
            # Meltwater exports wrap the Document ID in literal double-quotes
            # (e.g. "bTZx...") — strip them, or the tagging API silently no-ops
            # (returns 202 but matches no document).
            "document_id":   _first(r, cols["document_id"]).strip('"').strip(),
            "document_tags": _first(r, cols["document_tags"]),
            "keywords":      _first(r, cols["keywords"]),
        })
    return rows, cols


def read_export(path: str) -> tuple[list[dict], dict]:
    """Read an export file into (rows, detected_columns).

    Raises FileNotFoundError if the path is missing and ValueError if no URL
    column can be found (nothing to classify without it)."""
    if not os.path.exists(path):
        raise FileNotFoundError(path)

    ext = os.path.splitext(path)[1].lower()
    if ext == ".csv":
        df = pd.read_csv(path, dtype=str, keep_default_na=True)
    else:
        df = pd.read_excel(path, dtype=str)
    return rows_from_df(df)
