"""
Bring the DB-stored client-feedback rules into the classifier at run time.

feedback_rules (populated by the upload → extract flow) is LAYER 2 made live:
the classifier fetches the brand's ACTIVE rules and appends them to its system
prompt, so every run follows the latest client guidance without a code change.

Fails soft: if the DB isn't configured/reachable (e.g. an offline CLI run),
this returns an empty block and classification still works on the baked rules.

Cached per process with a short TTL so a batch of many articles hits the DB only
once, WHILE a newly uploaded/edited/deleted feedback rule still propagates within
TTL seconds — even to OTHER worker processes that never saw the mutation. The
upload/delete routes also call clear_cache() for instant effect on their own
worker. (A permanent cache used to hide new rules until the server restarted.)
"""

import os
import sys
import time

# (fetched_at, rules) per brand.
_cache: dict[str, tuple[float, list[dict]]] = {}

# Max seconds a cached rule set may be served before re-reading from the DB.
# Short enough that new client rules take effect quickly across all workers;
# long enough that one classify batch shares a single DB read.
CACHE_TTL_SECONDS = float(os.environ.get("BENTLEY_RULES_CACHE_TTL", "60"))


def _db():
    """Import the webapp db module (add its folder to sys.path first)."""
    here = os.path.dirname(__file__)
    proj = os.path.abspath(os.path.join(here, "..", ".."))
    webapp = os.path.join(proj, "webapp")
    for p in (proj, webapp):
        if p not in sys.path:
            sys.path.insert(0, p)
    import db
    return db


def get_rules(brand_name: str = "Bentley", use_cache: bool = True) -> list[dict]:
    """Return the brand's ACTIVE feedback rules (or [] if DB unavailable).

    Uses a TTL cache: a hit newer than CACHE_TTL_SECONDS is reused; otherwise the
    DB is re-read so freshly uploaded/edited rules propagate on their own."""
    if use_cache:
        hit = _cache.get(brand_name)
        if hit is not None and (time.time() - hit[0]) < CACHE_TTL_SECONDS:
            return hit[1]
    rules: list[dict] = []
    try:
        db = _db()
        if db.is_configured():
            rules = db.list_feedback_rules(brand_name, active_only=True)
    except Exception:
        rules = []  # offline / not configured — fall back to baked rules only
    _cache[brand_name] = (time.time(), rules)
    return rules


def rules_block(brand_name: str = "Bentley") -> tuple[str, int]:
    """Return (prompt_text_block, count) for the brand's active feedback rules.
    Empty string + 0 when there are none."""
    rules = get_rules(brand_name)
    if not rules:
        return "", 0
    lines = ["## LEARNED RULES FROM CLIENT FEEDBACK",
             "These refine the protocol above and OVERRIDE it on conflict. Follow them strictly."]
    for r in rules:
        cat = r.get("category") or "general"
        lines.append(f"- [{cat}] {r.get('rule_text', '').strip()}")
    return "\n".join(lines), len(rules)


def clear_cache() -> None:
    _cache.clear()
