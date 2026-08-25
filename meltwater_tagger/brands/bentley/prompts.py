"""
Bentley classification prompt + structured-output schema.

The system prompt is the client's Claude-Project instructions, adapted for the
pipeline (one article at a time, JSON out). The tag "menu" and the output schema
are generated from taxonomy.py + rules.py so they never drift out of sync with
the protocol. This is where LAYER 1 (taxonomy) and LAYER 2 (rules) come together
into the actual instructions Claude reads.
"""

from brands.bentley import taxonomy as tax
from brands.bentley import rules


# ---------------------------------------------------------------------------
# Build a compact, readable tag menu from the taxonomy for the prompt.
# ---------------------------------------------------------------------------
def _menu() -> str:
    lines = []

    def block(title, items, key="label", extra=("keywords", "definition", "hint")):
        lines.append(f"\n### {title}")
        for it in items:
            label = it[key]
            hints = []
            for e in extra:
                v = it.get(e)
                if v:
                    if isinstance(v, list):
                        v = ", ".join(v)
                    hints.append(str(v))
            suffix = f"  — {'; '.join(hints)}" if hints else ""
            lines.append(f"- {label}{suffix}")

    block("Type of Publication (pick ONE)", tax.TYPE_OF_PUBLICATION)
    block("Type of Coverage (pick ONE)", tax.TYPE_OF_COVERAGE)
    block("Region (pick ONE — by PUBLICATION country)", tax.REGION)
    block("Corporate (0+; General is last resort)", tax.CORPORATE)
    block("Pillar (0+)", tax.PILLAR)
    block("Industry (usually ONE)", tax.INDUSTRY)
    block("Product (0+; explicit name only)", tax.PRODUCT)
    lines.append("\n### Spokesperson (0+; only if quoted / a significant source)")
    lines.append("Use exactly 'Spokesperson | <Name>'. Known people include: "
                 + ", ".join(sp["name"] for sp in tax.SPOKESPEOPLE) + ".")
    return "\n".join(lines)


SYSTEM_PROMPT = """You are a Bentley Systems media-monitoring classification specialist applying \
Bentley's official Meltwater Tag Protocol. You classify ONE article at a time and return JSON.

## CORE PRINCIPLE — tag only what is SIGNIFICANTLY discussed
Do NOT assign a tag just because a keyword appears. A tag applies only when Bentley, its products, \
technologies, industries, spokespeople, or strategic themes are discussed in a meaningful, substantive way.
If Bentley appears only in passing — a company list, market-report name-drop, competitor roundup, \
attendee/sponsor list, or otherwise unrelated article — the item is generally "Not in Scope" unless \
Bentley is a material focus.

## MINIMAL TAGGING — assign the FEWEST tags that are genuinely warranted
Fewer, correct tags beat many loosely-related ones. Tag only the article's ACTUAL focus, not every
topic it touches. Concretely:
- Beyond the mandatory Type of Publication + Region, add a tag ONLY if that theme/product/person is a
  real subject of the piece — not merely mentioned.
- Industry: for in-scope infrastructure coverage assign exactly ONE — do NOT leave it empty. Pick the
  primary sector; when the piece is broad or cross-sector infrastructure (e.g. a general event covering
  transport + water + energy), use AEC as the general infrastructure catch-all (per the protocol's AEC
  definition). Add a second industry only if it genuinely spans two distinct sectors equally.
- Pillar: assign one only when that lens (AI / Connected Data / Resilience) is a real theme; often zero.
- Product: only products named explicitly in the text.
- Corporate - General: last resort; skip if any other corporate/industry tag already fits.
- When in doubt about a tag, LEAVE IT OFF. A short, precise tag set is the goal.

## PILLAR & PRODUCT & TECHNOLOGY — extra-high bar (the two most common over-tags)
- A **Pillar** tag (Infrastructure AI / Connected Data / Resilient Built World) requires the article to
  SUBSTANTIVELY explain that capability — HOW AI helps, HOW fragmented data is connected, HOW resilience/
  risk is addressed. If AI / connected data / digital twins are merely NAMED as themes — especially in
  event, conference, awards, or milestone coverage ("showcased AI, digital twins and connected data") —
  that is NOT substantive: assign NO Pillar tag. Often the correct number of Pillar tags is ZERO.
- **Corporate - Product & Technology** requires a SPECIFIC named Bentley product (MicroStation, iTwin,
  ProjectWise, SYNCHRO, Blyncsy, AssetWise, …). Generic phrases like "digital engineering technology" or
  "digital twins" with NO product name do NOT qualify — do not assign it, and do not assign any Product tag.

## NOT IN SCOPE (highest priority — decide this FIRST)
Classify as Not in Scope if any apply:
{not_in_scope}

## TYPE OF COVERAGE — Unique is the DEFAULT; Press release is the rare exception
Two independent ways to be **Unique** — EITHER is enough:
  (a) the article has a named author byline; OR
  (b) the publication produced its OWN reporting, analysis, summary, or commentary about Bentley — i.e. a
      third-party outlet writing ABOUT Bentley (Bentley is the subject), EVEN IF it is short, formulaic,
      and has NO named byline (e.g. a financial-news brief summarising Bentley's earnings with its own
      framing is Unique — it is the outlet's authored content, not Bentley's).
- **Press release** — ONLY when the content is BENTLEY-ISSUED: Bentley's own release, or a direct unedited
  pickup of a Bentley announcement with no editorial contribution. Signals: "Bentley Systems today
  announced…", a wire release Bentley distributed, listed on bentley.com/newsroom. A short third-party
  brief is NOT a press release.
- **3rd party press release** — a release issued by ANOTHER organisation where Bentley is merely mentioned
  (not the subject). Never for Bentley-issued content.
- Do NOT use "no byline" as a reason to pick Press release. Absence of a byline does not make it a release —
  most third-party coverage of Bentley is **Unique**. Choose Press release only on clear Bentley-issued signals.

## REGION — the PUBLICATION's country of origin (NOT the location in the story).
Determine it from the OUTLET — its name and its URL DOMAIN — exactly as a person would:
  - domain/TLD clues: sg.* or .sg -> Singapore (APAC); Gulf outlets / .sa / .ae -> Middle East (EMEA);
    .in -> India (APAC); .co.uk / .de / .fr / .za -> Europe/Africa (EMEA); .com.au -> Australia (APAC);
    US outlets (.com US-based) -> NALA.
  - Buckets: NALA = North & Latin America · EMEA = Europe, Middle East, Africa · APAC = Asia-Pacific.
Do NOT default to NALA, and do NOT use the country the STORY is about. If you genuinely cannot tell the
publication's country from the outlet/domain, leave Region EMPTY (it will be flagged for review) — never guess.

## CRITICAL QA CORRECTIONS (the traps the client keeps flagging)
{qa}

## TAG MENU — assign ONLY from this list; use the label EXACTLY as written
{menu}

## PUBLICATION & REGION — infer, NEVER default
Type of Publication and Region are normally inferable from the outlet + its domain, so INFER them from there.
Do NOT fall back to a "safe default" — no automatic NALA, no automatic Mainstream/Business. If, and ONLY if,
you genuinely cannot determine one from the outlet/domain, leave that field EMPTY; it will be flagged for a
human to fill (that is better than a wrong guess). Then add every other tag that genuinely applies.

## HOW TO ANSWER
Return the structured decision:
- decision: "In Scope" or "Not in Scope"
- reasoning: one short line on the scope decision
- the tag fields (leave a field empty if nothing applies; for Not-in-Scope, set type_of_coverage to "Not in scope" and leave the rest empty)
- qa_validation: keep it SHORT — 1-2 sentences total covering only the key judgment calls (e.g. a confused tag you deliberately did NOT assign). Do NOT write a justification for every tag; brevity matters for speed.
- uncertain: list any tag or decision you are NOT confident about (a genuine borderline call). Each entry = the tag + a few-word reason (e.g. "Industry - AEC (multi-sector, could be Energy)"). Still assign your best guess above; this just flags it so a human can confirm. Leave empty if you are confident.
""".format(
    not_in_scope="\n".join(f"- {t}" for t in rules.NOT_IN_SCOPE_TOPICS),
    qa="\n".join(f"- {c}" for c in rules.QA_CORRECTIONS),
    menu=_menu(),
)


# Per-article user message. Metadata (publication country, byline, source) is
# passed explicitly because Region and Type of Coverage depend on it.
ARTICLE_TEMPLATE = """Article to classify.

Source / outlet: {source}
Publication country: {pub_country}
Author byline: {byline}
URL: {url}

--- FULL ARTICLE TEXT ---
{text}
--- END ARTICLE TEXT ---

Classify this article per the Bentley protocol. Decide scope first, then assign all applicable tags."""


# ---------------------------------------------------------------------------
# Structured-output schema — single-pick families constrained by enum
# (generated from taxonomy so it can't drift); multi-pick families are arrays
# validated against the taxonomy after the call.
# ---------------------------------------------------------------------------
def _labels(group):
    return [it["label"] for it in group]


DECISION_SCHEMA = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": ["In Scope", "Not in Scope"]},
        "reasoning": {"type": "string", "description": "One line justifying the scope decision."},
        "type_of_publication": {
            "type": "string",
            "enum": _labels(tax.TYPE_OF_PUBLICATION) + [""],
            "description": "Outlet kind. Empty only if Not in Scope.",
        },
        "type_of_coverage": {
            "type": "string",
            "enum": _labels(tax.TYPE_OF_COVERAGE),
            "description": "Use 'Not in scope' when decision is Not in Scope.",
        },
        "region": {
            "type": "string",
            "enum": _labels(tax.REGION) + [""],
            "description": "By publication country of origin. Empty only if Not in Scope.",
        },
        "corporate": {
            "type": "array",
            "items": {"type": "string", "enum": _labels(tax.CORPORATE)},
            "description": "Zero or more Corporate tags.",
        },
        "pillar": {
            "type": "array",
            "items": {"type": "string", "enum": _labels(tax.PILLAR)},
        },
        "industry": {
            "type": "array",
            "items": {"type": "string", "enum": _labels(tax.INDUSTRY)},
            "description": "Usually exactly one.",
        },
        "product": {
            "type": "array",
            "items": {"type": "string", "enum": _labels(tax.PRODUCT)},
            "description": "Only products explicitly named in the text.",
        },
        "spokesperson": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Format 'Spokesperson | <Name>', only if quoted / a significant source.",
        },
        "qa_validation": {
            "type": "string",
            "description": "SHORT: 1-2 sentences on the key judgment calls only. Not a per-tag essay.",
        },
        "uncertain": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Tags/decisions you are NOT confident about (borderline) — each: tag + short reason. Empty if confident.",
        },
    },
    "required": ["decision", "reasoning", "type_of_coverage", "qa_validation"],
    "additionalProperties": False,
}
