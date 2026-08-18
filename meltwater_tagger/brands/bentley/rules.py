"""
Bentley tagging rules — LAYER 2 (the evolving, feedback-driven layer).

Two kinds of rule live here:

1. DETERMINISTIC rules the engine applies WITHOUT the LLM — because they are
   absolute and cheaper/safer to enforce in code (source block-list, "any
   Product tag also needs Corporate - Product & Technology", the mandatory
   Publication + Region tags).

2. JUDGMENT rules the LLM must follow — kept as text and injected into the
   system prompt (the QA corrections, the "significantly discussed" bar).

Seeded from the Bentley Meltwater Tag Protocol + the client's Claude-Project
instructions + the weekly "Tagging Adjustments" correction docs. When new
client feedback arrives, ADD to the lists here (and/or QA_CORRECTIONS) — no
engine change needed. That is the whole point of keeping this separate.
"""

# ---------------------------------------------------------------------------
# DETERMINISTIC — source block-list. If the article's source/domain matches,
# it is Not in Scope before we even call the LLM. (Case-insensitive substring
# match against the URL / source name.)
# ---------------------------------------------------------------------------
NOT_IN_SCOPE_DOMAINS = [
    "seequent.com",             # internal subsidiary source
    "newsbreak.com",            # client-flagged not-in-scope source
    "github.com",               # code repos / developer releases
    "pulse.bot",                # client-flagged not-in-scope source
    "vocal.media",              # client-flagged not-in-scope source
    "infrastructure-now.co.uk", # client-flagged not-in-scope source
]

# ---------------------------------------------------------------------------
# JUDGMENT — topics the LLM should treat as Not in Scope (needs reading, so
# these are guidance, not a hard block). Mirrors the "NOT IN SCOPE RULES".
# ---------------------------------------------------------------------------
NOT_IN_SCOPE_TOPICS = [
    "scientific / academic papers, whitepapers, technical studies, anything with an abstract or a downloadable full report",
    "downloadable market reports presented as research (abstract + 'download the report')",
    "registration / event-listing pages: webinar invites, conference/workshop registration, session agendas, 'register'/'join webinar' links",
    "sponsored content / advertorials / paid placements",
    "coverage primarily about a FORMER (ex-) Bentley employee",
    "content from Bentley-owned or subsidiary sites (e.g. Seequent.com)",
    "GitHub repositories and developer code releases",
    "coverage focused on 'SewerAI'",
    "security vulnerability notices, CVE/exploit databases, reverse-engineering reports",
    "Bentley mentioned only in passing: company lists, competitor roundups, attendee/sponsor lists, investor-report name-drops, unrelated articles",
]

# ---------------------------------------------------------------------------
# DETERMINISTIC — structural tagging rules enforced after the LLM answers.
# ---------------------------------------------------------------------------

# Every IN-SCOPE item must carry at least these families (the "minimum 2" rule:
# Type of Publication + Region are always assigned; other tags added on top).
MANDATORY_FAMILIES = ["type_of_publication", "region"]

# If ANY Product-family tag is assigned, also assign Corporate - Product & Technology.
PRODUCT_IMPLIES_CORP_PRODTECH = True
CORP_PRODUCT_TECH_LABEL = "Corporate - Product & Technology"


# ---------------------------------------------------------------------------
# JUDGMENT — the critical QA corrections, injected verbatim into the prompt.
# These are the "commonly confused" traps the client keeps flagging.
# ---------------------------------------------------------------------------
QA_CORRECTIONS = [
    "Digital Twin ≠ Product - iTwin. Only tag iTwin when the product name 'iTwin' is explicitly written.",
    "Mention of AI ≠ Pillar - Infrastructure AI. AI must be a genuine focus (innovation or 'do more with less'), not a passing mention.",
    "Mention of sustainability ≠ Corporate - Sustainability. If the real theme is resilience / risk reduction / asset longevity, use Pillar - Resilient Built World instead.",
    "Mention of water ≠ Industry - Water. The story must materially focus on water infrastructure.",
    "Mention of cities ≠ Industry - Cities. Same bar — must be a material focus.",
    "Product tags require the explicit product name. Never infer a product.",
    "Region = PUBLICATION country of origin, NOT the location discussed in the article.",
    "Bylined articles are usually Type of Coverage - Unique (even a press-release pickup with a byline).",
    "Any Product tag generally also needs Corporate - Product & Technology.",
    "Market reports / investor content about Bentley as a public company → Corporate - Financial / IR.",
    "Substations serving customers/utility networks = Energy - Electric Utilities; assets that GENERATE energy = Energy - Power Generation.",
    "Corporate - General is a last resort: skip it if any other corporate/industry tag fits, or if the Bentley mention is brief.",
    "Assign only ONE primary Industry tag unless the article genuinely spans multiple sectors.",
]


# ---------------------------------------------------------------------------
# Helpers the engine calls.
# ---------------------------------------------------------------------------
def blocked_source(url: str = "", source: str = "") -> str | None:
    """Return a reason string if the item is a hard 'Not in Scope' by source,
    else None. Deterministic — runs before the LLM."""
    hay = f"{url} {source}".lower()
    for dom in NOT_IN_SCOPE_DOMAINS:
        if dom in hay:
            return f"Source '{dom}' is a client-flagged not-in-scope source."
    return None


def enforce_structural_rules(tags_by_family: dict) -> dict:
    """Apply the deterministic post-LLM rules in place and return the result.

    - If any Product tag exists, ensure Corporate - Product & Technology is present.
    - If NO Product tag exists, remove Corporate - Product & Technology (a common
      over-tag: the model adds it on a generic "digital technology" mention with
      no specific named product). Product & Technology travels WITH a product.
    Returns the (possibly modified) tags_by_family dict. Does NOT invent the
    mandatory Publication/Region tags — those are validated/flagged separately
    because they depend on metadata the LLM may not have.
    """
    if PRODUCT_IMPLIES_CORP_PRODTECH:
        corp = tags_by_family.setdefault("corporate", [])
        has_product = bool(tags_by_family.get("product"))
        if has_product and CORP_PRODUCT_TECH_LABEL not in corp:
            corp.append(CORP_PRODUCT_TECH_LABEL)
        elif not has_product and CORP_PRODUCT_TECH_LABEL in corp:
            corp.remove(CORP_PRODUCT_TECH_LABEL)
    return tags_by_family


def missing_mandatory(tags_by_family: dict) -> list[str]:
    """Return the mandatory families that are empty (for review-flagging)."""
    missing = []
    for fam in MANDATORY_FAMILIES:
        val = tags_by_family.get(fam)
        if not val:
            missing.append(fam)
    return missing
