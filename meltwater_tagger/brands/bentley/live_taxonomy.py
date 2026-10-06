"""
Bring the UI-edited tag list (add / remove tags) into the classifier at run time.

The client adds or removes Bentley tags from the Brand studio page; those edits
are stored in Supabase (`taxonomy_overrides`, one JSON row per brand, same
format as taxonomy_overrides.json). sync() fetches that row and, when it has
changed, re-applies it on top of the base taxonomy and rebuilds the prompt +
output schema — so a new/removed tag takes effect without a code change.

Fails soft exactly like live_rules: DB unconfigured/unreachable -> the base
taxonomy is used. Cached per process with the same short TTL so one batch hits
the DB once, while edits still reach other workers within TTL seconds.
"""

import json
import threading
import time

from brands.bentley import taxonomy
from brands.bentley.live_rules import CACHE_TTL_SECONDS, _db

_lock = threading.Lock()
_fetched_at: float = 0.0
# JSON of the overrides currently applied. At import the taxonomy IS the base
# (no UI edits), so "{}" is already applied: with no DB edits, sync() never
# touches the taxonomy or the prompt — the classifier runs exactly as before.
_applied_key: str = "{}"


def fetch(brand_name: str = "Bentley") -> dict:
    """The brand's stored overrides dict ({} when none / DB unavailable)."""
    try:
        db = _db()
        if db.is_configured():
            return db.get_taxonomy_overrides(brand_name) or {}
    except Exception:
        pass
    return {}


def apply(ov: dict) -> None:
    """Apply `ov` to the taxonomy + rebuild prompts, if it differs from what is
    already applied."""
    global _applied_key
    key = json.dumps(ov or {}, sort_keys=True)
    with _lock:
        if key == _applied_key:
            return
        taxonomy.set_runtime_overrides(ov)
        from brands.bentley import prompts
        prompts.rebuild()
        _applied_key = key


def sync(brand_name: str = "Bentley", use_cache: bool = True) -> None:
    """Make the in-process taxonomy match the DB (at most once per TTL)."""
    global _fetched_at
    if use_cache and (time.time() - _fetched_at) < CACHE_TTL_SECONDS:
        return
    ov = fetch(brand_name)
    _fetched_at = time.time()
    apply(ov)


def removed_labels(ov: dict) -> set[str]:
    """Exact tag labels the client REMOVED in the UI (built-in tags under
    "remove" + removed spokespeople). Empty when there are no UI removals."""
    out: set[str] = set()
    for labels in (ov.get("remove") or {}).values():
        if isinstance(labels, list):
            out.update(l for l in labels if isinstance(l, str))
    for name in (ov.get("spokespeople_remove") or []):
        if isinstance(name, str):
            out.add(taxonomy.spokesperson_label(name))
    return out


def drop_removed_tags(results: list[dict], brand_name: str = "Bentley") -> tuple[list[dict], set[str]]:
    """Strip UI-removed tags from result rows before they are applied to
    Meltwater. Some tags are assigned by code rather than by the model (e.g.
    Corporate - Financial / IR, Corporate - Product & Technology, the coverage
    types), so removing them from the prompt alone does not stop them being
    applied — this does. Returns (rows, labels_dropped). With no UI removals
    (or the DB unavailable) the rows are returned unchanged."""
    removed = removed_labels(fetch(brand_name))
    if not removed:
        return results, set()
    out, dropped = [], set()
    for r in results:
        tags = r.get("tags") or []
        hit = [t for t in tags if t in removed]
        if hit:
            dropped.update(hit)
            r = {**r, "tags": [t for t in tags if t not in removed]}
        out.append(r)
    return out, dropped


def clear_cache() -> None:
    global _fetched_at
    _fetched_at = 0.0
