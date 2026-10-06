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
PRESS_RELEASE_LABEL = "Type of Coverage - Press Release"

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
    "SCOPE via finances (client-confirmed 2026-09): if Bentley Systems' OWN finances are discussed — its "
    "stock/share price, valuation, an analyst rating or coverage of 'Bentley Systems (NASDAQ: BSY)', "
    "earnings, or a 'is it time to buy' framing — the article is IN SCOPE and gets Corporate - Financial / "
    "IR, from ANY source, even if the Bentley mention is short. That is a MATERIAL finance discussion, NOT a "
    "passing mention. Only a bare name-in-a-vendor/competitor-list with NO finance discussion is a passing "
    "mention -> Not in Scope (the sole exception being openpr, which is always Financial/IR).",
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


# Regional / localized editions (e.g. mx.investing.com, uk.finance.yahoo.com) are
# translated duplicates of a story republished across a global site's per-country
# editions. Client rule (2026-09, confirmed): ALL regional articles are Not in
# Scope. Detected SOLIDLY by a DNS SUBDOMAIN LABEL that is an ISO country code —
# not a substring (so "brand.com" won't match "br") and NOT the TLD (so a genuine
# national outlet like it-boltwise.de is left alone). Runs before the financial-
# source rule so mx.investing.com is dropped while the main investing.com still
# gets Financial/IR.
_COUNTRY_CODES = set(
    "ad ae af ag al am ao ar at au aw az ba bb bd be bf bg bh bi bj bn bo br bs bt "
    "bw by bz ca cd cf cg ch ci cl cm cn co cr cu cv cy cz de dj dk dm do dz ec ee "
    "eg er es et fi fj fr ga gb ge gh gm gn gq gr gt gw gy hk hn hr ht hu id ie il in "
    "iq ir is it jm jo jp ke kg kh kr kw kz la lb lk lr ls lt lu lv ly ma mc md mg "
    "mk ml mn mo mr mt mu mv mw mx my mz na ne ng ni nl no np nz om pa pe pg ph pk "
    "pl pt py qa ro rs ru rw sa sc sd se sg si sk sl sn sr sv sy sz td tg th tj tn "
    "tr tt tw tz ua ug uk uy uz ve vn ye za zm zw".split()
)
# Codes deliberately EXCLUDED because they are far more often generic tech/word
# subdomains than a country edition (would false-flag): ai, tv, io, ad, ms, fm, me.
_COUNTRY_CODES -= {"ai", "tv", "io", "ms", "fm", "me"}


# A LOCALE code (language-country, e.g. en-gb, es-mx, fr-fr, pt-br, zh-cn) is an
# unambiguous regional-edition marker wherever it appears — a subdomain label or a
# path segment. Safe (nothing else has this exact shape). Deliberately NOT matched:
# a BARE country code in a PATH segment (e.g. /it/ = IT department, not Italy — a
# real false positive) and country ccTLDs (a genuine national outlet like
# it-boltwise.de is tracked, not an edition).
_LOCALE_RE = re.compile(r"^[a-z]{2}-[a-z]{2}$")


def is_regional_edition(url: str = "") -> bool:
    """True if the URL is a regional/localized edition. Pattern-based, so it applies
    to ANY site, not a hardcoded list:
      • a subdomain LABEL that is an ISO country code (mx.investing.com, uk.finance.yahoo.com), OR
      • a LOCALE code (xx-yy) as a subdomain label OR a path segment (es-mx.site.com, site.com/en-gb/…)."""
    from urllib.parse import urlparse
    try:
        p = urlparse(url or "")
    except Exception:
        return False
    host = (p.netloc or "").lower().split("@")[-1].split(":")[0]   # strip userinfo/port
    labels = [l for l in host.split(".") if l and l != "www"]
    subdomain_labels = labels[:-2] if len(labels) > 2 else []       # exclude domain + TLD
    if any(l in _COUNTRY_CODES for l in subdomain_labels):
        return True
    if any(_LOCALE_RE.match(l) for l in subdomain_labels):
        return True
    segs = [s for s in (p.path or "").lower().split("/") if s]
    if any(_LOCALE_RE.match(s) for s in segs):
        return True
    return False


# A finance / stock page: its host or path marks it as markets/investing content.
# Client rule (2026-09): do NOT drop such a page as a "syndicated stub" — in the
# Bentley feed it is there because it discusses Bentley's stock/finances, so it is
# Corporate - Financial / IR, not Not-in-scope. (Regional country-subdomain finance
# editions are already dropped earlier by is_regional_edition.)
_FINANCE_HOST_MARKERS = ("finance.", "investing.", "markets.", "money.", "marketwatch",
                         "stocktwits", "benzinga", "seekingalpha", "simplywall",
                         "tipranks", "zacks", "fool.", "stockstory")
_FINANCE_PATH_MARKERS = ("/stocks/", "/stock/", "/markets/", "/market/",
                         "/analyst-ratings/", "/quote/", "/investing/")


def is_finance_page(url: str = "") -> bool:
    """True if the URL is a finance / stock-markets page (host or path signals)."""
    from urllib.parse import urlparse
    try:
        p = urlparse(url or "")
    except Exception:
        return False
    host = (p.netloc or "").lower()
    path = (p.path or "").lower()
    return any(m in host for m in _FINANCE_HOST_MARKERS) or any(m in path for m in _FINANCE_PATH_MARKERS)


def is_listing_page(text: str = "") -> bool:
    """True if the extracted body is a category / archive / feed page — a stack
    of 'read more' teaser excerpts for several different stories, not a single
    article. Such pages end each excerpt with the WordPress-style truncation
    marker "[…]". Tagging the concatenation would merge unrelated stories
    (observed: a highway digital-twin URL whose body was six unrelated Bentley
    teasers — HR appointment, award, education MoU, power-grid, product news),
    so the caller flags it for manual review instead.

    A genuine article may carry a couple of trailing 'related posts' teasers, so
    the teasers must DOMINATE the body (≥3 of them AND ≥50% of all blocks)
    before we treat the extraction as a listing page."""
    if not text:
        return False
    blocks = [b.strip() for b in re.split(r"\n+", text) if b.strip()]
    if len(blocks) < 3:
        return False
    teasers = [b for b in blocks if b.endswith("[…]") or b.endswith("[...]")]
    if len(teasers) < 3:
        return False
    return len(teasers) / len(blocks) >= 0.5


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
