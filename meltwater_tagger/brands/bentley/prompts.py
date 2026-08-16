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
    lines.append("Use exactly 'Spokesperson - <Name>'. Known people include: "
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

## TYPE OF COVERAGE
- Unique: original author byline / original reporting or analysis (a press-release pickup WITH a byline is still Unique).
- Press release: a Bentley-issued release, or a direct pickup of a Bentley announcement with no editorial contribution.
- 3rd party press release: a release issued by ANOTHER organization where Bentley is merely mentioned. Never use this for Bentley-issued releases.

## REGION — publication country of origin, NOT the location in the story.
US publication → NALA · UAE/UK/Germany/South Africa → EMEA · India/Singapore/Australia/China/Cambodia → APAC.

## CRITICAL QA CORRECTIONS (the traps the client keeps flagging)
{qa}

## TAG MENU — assign ONLY from this list; use the label EXACTLY as written
{menu}

## MANDATORY for every IN-SCOPE article
Always assign Type of Publication AND Region (the minimum). Then add every other tag that genuinely applies.
(If you cannot tell Publication or Region from the text, still make your best guess and note it in qa_validation.)

## HOW TO ANSWER
Return the structured decision:
- decision: "In Scope" or "Not in Scope"
- reasoning: one short line on the scope decision
- the tag fields (leave a field empty if nothing applies; for Not-in-Scope, set type_of_coverage to "Not in scope" and leave the rest empty)
- qa_validation: justify each assigned tag briefly, AND explicitly state why any commonly-confused tag was NOT assigned (e.g. "digital twin present but 'iTwin' not named, so no Product - iTwin").
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
            "description": "Format 'Spokesperson - <Name>', only if quoted / a significant source.",
        },
        "qa_validation": {
            "type": "string",
            "description": "Why each assigned tag qualifies, and why confused tags were NOT assigned.",
        },
    },
    "required": ["decision", "reasoning", "type_of_coverage", "qa_validation"],
    "additionalProperties": False,
}
