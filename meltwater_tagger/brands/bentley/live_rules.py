"""
Bring the DB-stored client-feedback rules into the classifier at run time.

feedback_rules (populated by the upload → extract flow) is LAYER 2 made live:
the classifier fetches the brand's ACTIVE rules and appends them to its system
prompt, so every run follows the latest client guidance without a code change.

Fails soft: if the DB isn't configured/reachable (e.g. an offline CLI run),
this returns an empty block and classification still works on the baked rules.
Cached per process so a batch of many articles hits the DB only once.
"""

import os
import sys

_cache: dict[str, list[dict]] = {}


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
    """Return the brand's ACTIVE feedback rules (or [] if DB unavailable)."""
    if use_cache and brand_name in _cache:
        return _cache[brand_name]
    rules: list[dict] = []
    try:
        db = _db()
        if db.is_configured():
            rules = db.list_feedback_rules(brand_name, active_only=True)
    except Exception:
        rules = []  # offline / not configured — fall back to baked rules only
    _cache[brand_name] = rules
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
