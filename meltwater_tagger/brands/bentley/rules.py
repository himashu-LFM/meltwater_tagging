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

import re

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

# Every IN-SCOPE item must carry at least these families: Type of Publication,
# Type of Coverage, and Region are always assigned; other tags added on top. An
# empty one is flagged for review. (Financial/IR items intentionally suppress
# Publication + Coverage per the client rule — classify_bentley handles their
# review check separately, so it never calls this for them.)
MANDATORY_FAMILIES = ["type_of_publication", "type_of_coverage", "region"]

# If ANY Product-family tag is assigned, also assign Corporate - Product & Technology.
PRODUCT_IMPLIES_CORP_PRODTECH = True
CORP_PRODUCT_TECH_LABEL = "Corporate - Product & Technology"


# ---------------------------------------------------------------------------
# Deterministic Bentley-ISSUED press-release detection.
#
# A Bentley press release is often republished verbatim on a third-party site or
# wire (citybiz, MENAFN, PR Newswire, …), sometimes with a dateline or auto-
# byline. The model then tends to call it "Unique", but the CONTENT is
# Bentley-issued, so the protocol (and the client) call it a Press release.
#
# The signal MUST be that BENTLEY ITSELF is the announcer, near the top (the
# dateline). Earlier markers ("Nasdaq: BSY", the "infrastructure engineering
# software company" About-boilerplate) were too weak: a THIRD-PARTY release that
# merely quotes Bentley's About section (e.g. "Naviam … to acquire Cohesive from
# Bentley Systems …") carries them too, and was wrongly tagged Press release
# instead of 3rd party press release. So we now require "Bentley Systems" to sit
# right BEFORE an announcement verb (Bentley as the subject that announced), which
# a partner-issued release does not (there Bentley appears AFTER "acquire … from").
# ---------------------------------------------------------------------------
PRESS_RELEASE_LABEL = "Type of Coverage - Press release"

# "Bentley Systems (Nasdaq: BSY), the infrastructure engineering software company,
#  today announced …"  ->  Bentley is the announcer. The {0,160} span skips the
# parenthetical/boilerplate; [^.] keeps it within one sentence so a later,
# unrelated sentence can't bridge to the verb.
_BENTLEY_ISSUER_RE = re.compile(
    r"bentley systems\b[^.]{0,160}?\b(?:today\s+)?"
    r"(?:announced|announces|unveiled|unveils|launched|launches|"
    r"released|releases|introduced|introduces)\b",
    re.I | re.S,
)


def is_bentley_press_release(text: str) -> bool:
    """True only if BENTLEY is the announcer near the top of the item (a genuine
    Bentley-issued release / verbatim pickup). Deliberately high-precision: when
    unsure we return False and let it fall to Unique / 3rd party, because most
    no-byline coverage of Bentley is NOT a Bentley-issued release."""
    head = (text or "")[:900]
    return bool(_BENTLEY_ISSUER_RE.search(head))


# ---------------------------------------------------------------------------
# JUDGMENT — the critical QA corrections, injected verbatim into the prompt.
# These are the "commonly confused" traps the client keeps flagging.
# ---------------------------------------------------------------------------
QA_CORRECTIONS = [
    "Digital Twin ≠ Product - iTwin. Only tag iTwin when the product name 'iTwin' is explicitly written.",
    "Mention of AI ≠ Pillar - Infrastructure AI. AI must be a genuine focus (innovation or 'do more with less'), not a passing mention.",
    "Mention of sustainability ≠ Corporate - Sustainability. If the real theme is resilience / risk reduction / asset longevity, use Pillar - Resilient Built World instead.",
    "Mention of water ≠ Industry | Water. The story must materially focus on water infrastructure.",
    "Mention of cities ≠ Industry | Cities. Same bar — must be a material focus.",
    "Product tags require the explicit product name. Never infer a product.",
    "Region = PUBLICATION country of origin, NOT the location discussed in the article. (e.g. a Cambodia project covered by a US outlet is NALA; a UAE outlet is EMEA.)",
    "Coverage type is decided by the AUTHOR BYLINE: a byline → Type of Coverage - Unique (even a press-release pickup with a byline). NO byline + the release was issued BY Bentley → Press release. NO byline + issued by ANOTHER organisation (Bentley only mentioned) → 3rd party press release. A Bentley-issued release is listed on bentley.com/newsroom.",
    "Bentley subsidiaries/brands (Seequent, Blyncsy, Cohesive) count AS Bentley for scope — coverage about them is IN scope. (Only content published ON a subsidiary's own site, e.g. seequent.com, is a not-in-scope source.)",
    "A competitor story (Autodesk, Hexagon, Nemetschek, Trimble, …) that mentions Bentley only inside a 3rd-party press release → Type of Coverage - 3rd party press release + Region + Type of Publication ONLY; do NOT add Industry/Pillar/Corporate unless Bentley is a material theme.",
    "Being named in Bentley's ecosystem/partner catalog (another company chosen for the catalog) is NOT Corporate - Events/Milestones/Awards.",
    "Any Product tag generally also needs Corporate - Product & Technology.",
    "Corporate - Financial / IR applies ONLY when Bentley's mention sits in content whose PURPOSE is "
    "financial / investment / market analysis: earnings or results (Bentley's own, or Bentley in a peer-"
    "comparison earnings piece); stock / analyst commentary (price targets, ratings, buy/sell/hold); "
    "investment-platform content (Simply Wall St, TipRanks, StockStory, Zacks, Investing.com naming Bentley "
    "as a peer or comparison); Bentley's own dividend announcements; market-cap / valuation / undervalued-"
    "stock lists; or investor-letter / fund-holding citations. It does NOT apply to: a general business/"
    "trade article that mentions money once such as a contract award or product-launch pricing (that is "
    "Corporate - Product & Technology or Corporate - Events); M&A such as Naviam/Cohesive (that is "
    "Corporate - M&A, NEVER Financial/IR, even though it is financial in nature); or a company's revenue / "
    "market-cap mentioned as background/credibility context in a non-financial story (that stays "
    "Corporate - Events/Milestones/Awards).",
    "Substations serving customers/utility networks = Energy - Electric Utilities; assets that GENERATE energy = Energy - Power Generation.",
    "Corporate - General is a last resort: skip it if any other corporate/industry tag fits, or if the Bentley mention is brief.",
    # Repeatedly flagged: the model mislabels awards/event stories as HR. Corporate "
    # categories are distinct — match the STORY, not a keyword.
    "HR vs EVENTS is the most-confused Corporate pair — use this test. If the story is about a PERSON's "
    "role (hired, promoted, departing, or APPOINTED/ELECTED/NAMED to a board of directors) or about "
    "workplace/culture/philanthropy -> Corporate - HR / Colleague Success. A board-of-directors "
    "appointment is HR EVEN THOUGH it reads like an honor (e.g. 'appointed to the OGC Board of Directors' "
    "= HR, not Events). If the story is about a WIN/award/ranking/recognition, an anniversary, an office/hub "
    "OPENING, an event, or a company/product milestone -> Corporate - Events/Milestones/Awards (a company "
    "or product winning or being recognized is Events, not HR). Never put the same fact in both. "
    "Corporate - Education is ONLY STEM/STEAM, engineering resources, or education awards.",
    # Repeatedly flagged: finance/markets outlets mis-tagged as Technology.
    "Type of Publication is the OUTLET's nature, not the topic: a finance / markets / investing outlet "
    "(e.g. MarketScreener, Investing.com, a stock-news wire) is Mainstream/Business, NOT Technology. "
    "Technology is only for technology-focused publications; sector trade magazines are Trade/Industry.",
    "Industry is OPTIONAL (client-confirmed 2026-09): assign an Industry tag only when the article has a "
    "material industry theme; leave it EMPTY for corporate-only, pillar-only, or financial-only items. "
    "Never assign more than one unless the piece genuinely spans two sectors equally.",
    # Client clarification (2026-09) on Seequent/Bentley product mentions in third-party stories:
    "A named Bentley/Seequent product (e.g. Leapfrog Geo, Seequent Imago, MX Deposit, PLAXIS, "
    "MicroStation) that is described as being USED or APPLIED — even briefly, even inside another "
    "company's own press release or a technical/regulatory-compliance section (e.g. 'resource data "
    "was validated in Leapfrog Geo', \"logged into Seequent's Imago\") — makes the article IN SCOPE. "
    "Tag the relevant Industry (e.g. Industry | Mining) and the coverage type. BUT if the Bentley/"
    "Seequent product is only NAMED IN A LIST among competing vendors/tools with no use or application "
    "described, that alone is NOT a reason to be in scope — treat as Not in Scope unless Bentley is a "
    "material focus for some other reason.",
    # Client guidance (2026-09) — AEC is the single most over-used tag.
    "Industry | AEC ONLY when the article is substantively about a real construction/engineering PROJECT or "
    "practice with sustained depth (a named project, an actual application). Do NOT assign AEC when the story "
    "is really about a PRODUCT/feature, a company MILESTONE or office opening, an AWARD, an EDUCATION/MoU "
    "initiative, a generic product capability with no real project, or spans multiple verticals with no "
    "dominant one. A Bentley product named WITH a real project in use -> AEC; a product merely LISTED -> not AEC.",
    # Client-confirmed change (2026-09): Product & Technology no longer requires a NAMED product.
    "Corporate - Product & Technology applies when a specific Bentley product is named OR when Bentley's own "
    "software/technology is discussed SUBSTANTIVELY (e.g. the operational value of Bentley's digital twins) "
    "even without a named product. A bare passing 'technology' mention still does not qualify. A Product-"
    "family tag still needs an explicit product name, so an item can have Corporate - Product & Technology "
    "with no Product tag.",
    # Pillars — tie to Bentley, judge the OVERALL message.
    "A Pillar tag needs the capability to be the article's OVERALL message and tied to BENTLEY, not a stray "
    "phrase in a quote and not just because Bentley is named elsewhere. Infrastructure AI = Bentley's OWN AI "
    "empowering engineers, NOT a tech-list item and NOT industry AI survey statistics. Connected Data = "
    "fragmented->unified single-source-of-truth as the main point, not an aside in an event/milestone piece. "
    "Resilient Built World = risk mitigation / predictive maintenance / asset longevity, NOT sustainability, "
    "NOT efficiency, NOT AI.",
]


# ---------------------------------------------------------------------------
# DETERMINISTIC — source-level Corporate - Financial / IR. Client rule (2026-09):
# openpr.com coverage is ALWAYS tagged Corporate - Financial / IR, even when
# Bentley is only mentioned in passing (unlike every other source, where a
# passing mention is Not in Scope). Emitted as Region + Financial/IR only (the
# Financial/IR suppression), with no LLM call.
# ---------------------------------------------------------------------------
FINANCIAL_IR_SOURCES = [
    "openpr.com",                 # client-confirmed (2026-09)
    # Pure investment / stock-analysis platforms the client's Financial/IR rule
    # names explicitly as Financial/IR content. They are heavily bot-walled, so a
    # fetch just yields a paywall -> review and the item is LOST — tag by source
    # instead (they are always financial coverage by nature). "investing.com" as a
    # substring also covers every country subdomain (il./sa./fr./br./pl./uk. …).
    # NOTE: inferred from the client's purpose-test list; confirm alongside openpr.
    "investing.com",
    "simplywall.st",
    "tipranks.com",
    "stockstory.org",
    "zacks.com",
    "webull.com",                 # confirmed by test: financial/markets platform, was being dropped
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


# Job / careers listings are not media coverage -> Not in Scope. A job posting can
# name Bentley software (STAAD, MicroStation) as a required skill, which would
# otherwise look in-scope. Deterministic, URL-based, runs before any fetch.
_JOBS_HOST_PREFIXES = ("careers.", "jobs.", "job.")
_JOBS_PATH_MARKERS = ("/job/", "/careers/")


def is_jobs_page(url: str = "") -> bool:
    """True if the URL is a job posting / careers listing (not coverage)."""
    from urllib.parse import urlparse
    try:
        p = urlparse(url or "")
    except Exception:
        return False
    host = (p.netloc or "").lower().replace("www.", "")
    path = (p.path or "").lower()
    return host.startswith(_JOBS_HOST_PREFIXES) or any(m in path for m in _JOBS_PATH_MARKERS)


def is_financial_ir_source(url: str = "", source: str = "") -> str | None:
    """Return the matched source string if the item is a source-level
    Corporate - Financial / IR (e.g. openpr.com), else None. Deterministic —
    runs before the LLM, like blocked_source."""
    hay = f"{url} {source}".lower()
    for dom in FINANCIAL_IR_SOURCES:
        if dom in hay:
            return dom
    return None


def enforce_structural_rules(tags_by_family: dict) -> dict:
    """Apply the deterministic post-LLM rules in place and return the result.

    - If any Product tag exists, ensure Corporate - Product & Technology is present.
    - Client-confirmed change (2026-09): Corporate - Product & Technology MAY stand
      ALONE when Bentley's own software/technology is discussed substantively even
      without a named product, so we NO LONGER strip it when no Product tag exists —
      the model's judgment (guided by the prompt's P&T definition) is trusted.
    Returns the (possibly modified) tags_by_family dict. Does NOT invent the
    mandatory Publication/Region tags — those are validated/flagged separately
    because they depend on metadata the LLM may not have.
    """
    if PRODUCT_IMPLIES_CORP_PRODTECH:
        corp = tags_by_family.setdefault("corporate", [])
        if tags_by_family.get("product") and CORP_PRODUCT_TECH_LABEL not in corp:
            corp.append(CORP_PRODUCT_TECH_LABEL)
    return tags_by_family


def missing_mandatory(tags_by_family: dict) -> list[str]:
    """Return the mandatory families that are empty (for review-flagging)."""
    missing = []
    for fam in MANDATORY_FAMILIES:
        val = tags_by_family.get(fam)
        if not val:
            missing.append(fam)
    return missing
