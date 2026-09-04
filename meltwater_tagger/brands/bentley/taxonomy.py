"""
Bentley tag taxonomy — the *label library*.

Built from "Bentley Meltwater Tag Protocol_Updated 2026.xlsx"
(sheet "Tag Protocol - Updated Feb. 26").

This is LAYER 1 (the stable protocol). It answers: which tags exist, what each
means, and the trigger words / products / people that hint at it. The evolving
client-feedback judgment calls live separately in `rules.py` (LAYER 2).

Unlike Kaseya (one sentiment tag per post), Bentley applies MANY tags per
article across several families, and first decides in-scope vs not-in-scope.

────────────────────────────────────────────────────────────────────────────
✅  EXACT MELTWATER TAG STRINGS — RECONCILED FROM THE LIVE TAG MODAL (Aug 2026)
Every `label` below is now the EXACT string as it appears in Meltwater's
Tag-content modal, captured from the live account and verified 2026-08-21. This
is the source of truth for BOTH classify (Phase 1) and apply (Phase 2), so the
strings can be applied to Meltwater character-for-character.

Separators are NOT uniform in Meltwater — reproduce them exactly:
  • hyphen " - "  →  Type of Publication, Type of Coverage, Corporate, Region,
                     Pillar, Product
  • pipe   " | "  →  Industry (multi-level, e.g. "Industry | Energy | Electric
                     Utilities") and Spokesperson ("Spokesperson | <Name>")
Note the sheet used an en dash "–" for Publication/Coverage, but Meltwater uses
a plain hyphen "-" — Meltwater wins. The Pillar typo "Resillient Built World" is
real in Meltwater too, so it is preserved here deliberately.

Some Bentley spokespeople named in the protocol sheet's context columns have NO
tag in Meltwater yet (see `no_mw_tag`). They are kept here for text detection,
but apply must NOT try to apply them (there is no tag to check) — flag them for
review / ask the client to create the tag instead.
────────────────────────────────────────────────────────────────────────────
"""

RUN_BRAND = "Bentley"

# ---------------------------------------------------------------------------
# Family metadata: is a family "single-pick" (choose at most one) or can an
# article carry several tags from it? Drives both the prompt and validation.
# ---------------------------------------------------------------------------
FAMILIES = {
    "type_of_publication": {"label": "Type of Publication", "multi": False},
    "type_of_coverage":    {"label": "Type of Coverage",    "multi": False},
    "region":              {"label": "Region",              "multi": False},
    "corporate":           {"label": "Corporate",           "multi": False},  # "exclusive to other types"
    "pillar":              {"label": "Pillar",              "multi": True},
    "industry":            {"label": "Industry",            "multi": False},  # "exclusive of each other"
    "product":             {"label": "Product",             "multi": True},
    "spokesperson":        {"label": "Spokesperson",        "multi": True},
}

# ---------------------------------------------------------------------------
# TYPE OF PUBLICATION — what kind of outlet is it. (metadata/outlet-driven)
# ---------------------------------------------------------------------------
TYPE_OF_PUBLICATION = [
    {"key": "pub_mainstream", "label": "Type of Publication - Mainstream/Business"},
    {"key": "pub_technology", "label": "Type of Publication - Technology"},
    {"key": "pub_trade",      "label": "Type of Publication - Trade/Industry"},
]

# ---------------------------------------------------------------------------
# TYPE OF COVERAGE — where the item came from. (byline / distributor-driven)
#   Unique              = a journalist wrote it (has an author byline)
#   Press release       = distributed by Bentley Systems themselves
#   3rd party press rel. = a press release from someone NOT Bentley
#   Not in scope        = do not track (see rules.py for the many triggers)
# ---------------------------------------------------------------------------
TYPE_OF_COVERAGE = [
    {"key": "cov_unique",      "label": "Type of Coverage - Unique",
     "hint": "has an author byline / original journalism"},
    {"key": "cov_press",       "label": "Type of Coverage - Press release",
     "hint": "issued by Bentley Systems (listed on bentley.com/newsroom)"},
    {"key": "cov_3rd_party",   "label": "Type of Coverage - 3rd party press release",
     "hint": "a press release from a party other than Bentley Systems"},
    {"key": "cov_not_in_scope", "label": "Not in scope",
     "hint": "do not track — see rules.py NOT_IN_SCOPE_* for triggers"},
]

# ---------------------------------------------------------------------------
# REGION — assigned from the PUBLICATION's country of origin (Meltwater-
# assigned), NOT the country mentioned in the article body. (metadata-driven)
# ---------------------------------------------------------------------------
REGION = [
    {"key": "region_nala", "label": "Region - NALA", "hint": "North America & Latin America (e.g. US, Canada, Brazil)"},
    {"key": "region_emea", "label": "Region - EMEA", "hint": "Europe, Middle East, Africa (e.g. UK, Germany, UAE, South Africa)"},
    {"key": "region_apac", "label": "Region - APAC", "hint": "Asia-Pacific (e.g. India, Australia, China, Cambodia)"},
]

# ---------------------------------------------------------------------------
# CORPORATE — about the COMPANY Bentley. "Exclusive to other types": use a
# corporate tag only when there IS a corporate focus; General is the fallback,
# and General is NOT added when a more specific corporate/industry tag applies.
# ---------------------------------------------------------------------------
CORPORATE = [
    {"key": "corp_hr",        "label": "Corporate - HR / Colleague Success",
     "keywords": ["exec hire", "new executive", "board of directors", "workplace", "culture", "philanthropy", "colleague"]},
    {"key": "corp_ma",        "label": "Corporate - M&A",
     "keywords": ["acquisition", "acquire", "merger", "M&A"]},
    {"key": "corp_financial", "label": "Corporate - Financial / IR",
     "keywords": ["financials", "quarterly earnings", "market report", "dividend", "investor", "IR", "stock", "share price", "revenue"]},
    {"key": "corp_product",   "label": "Corporate - Product & Technology",
     "keywords": ["product", "technology", "software", "launch", "release", "platform"],
     "note": "Add whenever any Bentley product is named. ALSO add alongside any Product-family tag."},
    {"key": "corp_gov",       "label": "Corporate - Government / Policy Work",
     "keywords": ["government", "policy", "infrastructure spending", "Transforming Infrastructure Performance", "TIP", "government relations"]},
    {"key": "corp_sustain",   "label": "Corporate - Sustainability",
     "keywords": ["ESG", "sustainability", "sustainable", "SDG", "carbon", "net zero"]},
    {"key": "corp_education",  "label": "Corporate - Education",
     "keywords": ["STEM", "STEAM", "engineering resources", "education award", "Enactus", "students", "university"]},
    {"key": "corp_csr",       "label": "Corporate - CSR/Compliance/Donation",
     "keywords": ["data standards", "security standards", "privacy policy", "legal", "ISO", "encryption", "Trust Center",
                  "DEI", "diversity", "inclusion", "equity", "Women in Engineering", "donation"]},
    {"key": "corp_events",    "label": "Corporate - Events/Milestones/Awards",
     "keywords": ["conference", "event", "summit", "award", "milestone", "accelerator", "catalog", "recognized", "keynote"]},
    {"key": "corp_general",   "label": "Corporate - General",
     "keywords": [],
     "note": "FALLBACK ONLY. Use for items with an obvious Bentley corporate angle that fit no category above. "
             "Do NOT use if any other corporate OR industry tag applies, or if the mention is too brief."},
]

# ---------------------------------------------------------------------------
# PILLAR — Bentley's three strategic themes (an article can carry more than one).
# The bar is about the STORY'S LENS, not just a keyword being present.
# ---------------------------------------------------------------------------
PILLAR = [
    {"key": "pillar_ai",        "label": "Pillar - Infrastructure AI",
     "keywords": ["artificial intelligence", "AI", "machine learning", "automation", "AI-powered"],
     "definition": "AI embedded across the portfolio to empower engineers — automating tedious tasks, "
                   "doing more with less. Tag when AI is discussed as innovation/technology OR practical "
                   "efficiency. NOT just because the word 'AI' appears."},
    {"key": "pillar_connected", "label": "Pillar - Connected Data",
     "keywords": ["connected data", "data environment", "siloed data", "fragmented data", "single source of truth",
                  "BIM", "GIS", "interoperability", "data reuse", "collaboration"],
     "definition": "Solving data being incomplete/inaccessible/siloed — an open, integrated environment giving "
                   "the right people the right data at the right time. Tag when the story frames the problem as "
                   "disconnected/fragmented data and Bentley tech as the connective solution."},
    {"key": "pillar_resilient", "label": "Pillar - Resillient Built World",
     "keywords": ["resilience", "resilient", "risk assessment", "predictive maintenance", "extend asset life",
                  "structural risk", "subsurface", "digital twin monitoring", "downtime", "recovery"],
     "definition": "Safe, sustainable, long-lasting infrastructure: predicting/preventing instability, extending "
                   "asset life, assessing/mitigating risk (surface + subsurface). Tag when Bentley tech is used to "
                   "increase asset longevity or assess/reduce structural/operational risk."},
]

# ---------------------------------------------------------------------------
# INDUSTRY — "exclusive of each other" (pick the single best-fit industry).
# keywords are the strongest signals distilled from the protocol; products help.
# ---------------------------------------------------------------------------
INDUSTRY = [
    {"key": "ind_aec", "label": "Industry | AEC",
     "keywords": ["construction", "construction planning", "construction design", "engineering firm",
                  "megaproject", "building", "owner operator", "BIM", "project management", "supply chain"],
     "products": ["MicroStation", "STAAD", "RAM", "ADINA", "OpenBuildings", "ProStructures", "SYNCHRO"]},
    {"key": "ind_cities", "label": "Industry | Cities",
     "keywords": ["cities", "urban planning", "smart cities", "campus", "municipal", "local government",
                  "public works", "mobility simulation", "city infrastructure"],
     "products": ["MicroStation", "OpenCities Map", "OpenCities Planner", "OpenPlant", "PlantSight"]},
    {"key": "ind_energy_utilities", "label": "Industry | Energy | Electric Utilities",
     "keywords": ["electric utilities", "transmission", "distribution", "power grid", "grid reliability",
                  "smart grid", "electrification", "dam monitoring", "distribution network"],
     "products": ["MicroStation", "PLS", "SPIDA", "OpenUtilities", "EasyPower", "OpenWindpower", "SACS", "MOSES", "MAXSURF", "AutoPIPE"]},
    {"key": "ind_energy_powergen", "label": "Industry | Energy | Power Generation",
     "keywords": ["power generation", "power plant", "energy production", "renewable energy", "wind", "solar",
                  "offshore", "hydrocarbon", "geothermal", "oil & gas", "nuclear", "SMR", "decarbonization", "energy transition"],
     "products": ["MicroStation", "EasyPower", "SACS", "MOSES", "MAXSURF", "AutoPIPE", "AssetWise", "PlantSight", "OpenPlant", "Leapfrog Energy"]},
    {"key": "ind_mining", "label": "Industry | Mining",
     "keywords": ["mining", "critical minerals", "mine planning", "mineral exploration", "resource estimation",
                  "ore body", "geological modeling", "drillhole", "geoscience", "subsurface", "open pit"],
     "products": ["Seequent", "Leapfrog", "Plaxis", "MicroStation", "Evo"]},
    {"key": "ind_trans_rail", "label": "Industry | Transportation | Rail & Transit",
     "keywords": ["rail", "metro", "transit", "track", "signaling", "freight rail", "railway", "ERTMS", "powerline"],
     "products": ["OpenRail", "OpenTunnel", "OpenBridge", "OpenBuildings", "OpenPaths", "AssetWise", "ProjectWise", "SYNCHRO", "Blyncsy"]},
    {"key": "ind_trans_airports", "label": "Industry | Transportation | Airports & Ports",
     "keywords": ["airport", "port", "runway", "passenger experience", "throughput", "harbour", "harbor"],
     "products": ["MicroStation", "ProjectWise", "SYNCHRO", "iTwin Experience", "iTwin Capture",
                  "OpenCities Planner", "OpenRoads", "OpenBuildings", "STAAD", "Blyncsy"]},
    {"key": "ind_trans_roads", "label": "Industry | Transportation | Roads & Highways",
     "keywords": ["road", "highway", "traffic simulation", "road asset", "autonomous vehicle", "road monitoring", "civil engineering"],
     "products": ["OpenRoads", "OpenBridge", "OpenTunnel", "SUPERLOAD", "OpenSite", "OpenSite+", "Blyncsy"]},
    {"key": "ind_trans_bridges", "label": "Industry | Transportation | Bridges & Tunnels",
     "keywords": ["bridge", "tunnel", "geotechnical", "predictive maintenance", "rehabilitation", "deficient bridge"],
     "products": ["OpenBridge", "OpenTunnel", "Bentley Infrastructure Cloud", "PLAXIS 2D", "PLAXIS 3D",
                  "Leapfrog", "OpenGround", "iTwin Capture", "iTwin IoT", "AssetWise"]},
    {"key": "ind_water", "label": "Industry | Water",
     "keywords": ["hydraulics", "hydrology", "water", "wastewater", "stormwater", "smart water", "water loss",
                  "desalination", "water utility", "flood"],
     "products": ["MicroStation", "OpenFlows", "OpenFlows WaterSight"]},
]

# ---------------------------------------------------------------------------
# PRODUCT — a Bentley product named in the item. (An item can name several.)
# IMPORTANT PROTOCOL RULE: whenever any Product tag is applied, ALSO apply
# "Corporate - Product & Technology". (Enforced in rules.py.)
# `aliases` = strings to look for in text; some products roll up to a headline
# product (e.g. iTwin Capture -> iTwin).
# ---------------------------------------------------------------------------
PRODUCT = [
    {"key": "prod_microstation", "label": "Product - MicroStation",
     "aliases": ["MicroStation"]},
    {"key": "prod_bic", "label": "Product - Bentley Infrastructure Cloud",
     "aliases": ["Bentley Infrastructure Cloud"],
     "note": "Umbrella of ProjectWise (design) + SYNCHRO (build) + AssetWise (operate). "
             "Match ONLY the literal umbrella name — the sub-products have their own tags, "
             "so aliasing them here would double-tag."},
    {"key": "prod_itwin", "label": "Product - iTwin",
     "aliases": ["iTwin", "iTwin Capture", "iTwin IoT", "iTwin Experience", "iTwin Studio", "iTwin Platform"],
     "note": "Digital-twin platform. NOTE: a generic 'digital twin' mention is NOT enough — the specific "
             "'iTwin' product name must appear (a repeated client correction)."},
    {"key": "prod_opensite", "label": "Product - OpenSite+", "aliases": ["OpenSite+"]},
    {"key": "prod_projectwise", "label": "Product - ProjectWise", "aliases": ["ProjectWise"]},
    {"key": "prod_assetwise", "label": "Product - AssetWise", "aliases": ["AssetWise"]},
    {"key": "prod_synchro", "label": "Product - SYNCHRO", "aliases": ["SYNCHRO"]},
    {"key": "prod_blyncsy", "label": "Product - Blyncsy", "aliases": ["Blyncsy"]},
    {"key": "prod_synchro_plus", "label": "Product - SYNCHRO+", "aliases": ["SYNCHRO+"]},
    {"key": "prod_openutilities_sub", "label": "Product - OpenUtilities Substation+", "aliases": ["OpenUtilities Substation+", "OpenUtilities Substation"]},
    {"key": "prod_staad", "label": "Product - STAAD", "aliases": ["STAAD"]},
]

# ---------------------------------------------------------------------------
# SPOKESPERSON — a Bentley person quoted in the item gets their tag.
# `context` (where the sheet noted one) hints which corporate/industry area
# that person usually speaks for — useful as a secondary signal only.
# De-duplicated across the sheet's per-industry lists.
# ---------------------------------------------------------------------------
SPOKESPEOPLE = [
    {"name": "Nicholas Cumins", "context": "CEO; Corporate - General / Product & Technology", "aliases": ["Nicolas Cumins"]},
    {"name": "Julien Moutte", "context": "emerging tech / Infrastructure AI"},
    {"name": "James Lee"},
    {"name": "Patrick Cozzi"},
    {"name": "Brock Ballard", "context": "Corporate - General"},
    {"name": "Cate Lochead"},
    {"name": "Ruth Sleeter"},
    {"name": "Jim Dobbs"},
    {"name": "Zeljko Djuretic", "context": "Education"},
    {"name": "Rachel Rogers"},
    {"name": "Chris Bradshaw", "context": "Education, Sustainability"},
    {"name": "Rodrigo Fernandes", "context": "Sustainability"},
    {"name": "Werner Andre", "context": "Financial / IR"},
    {"name": "Florence Zheng", "context": "HR / Colleague Success"},
    {"name": "Tom Kurke", "context": "M&A"},
    {"name": "Mark Coates", "context": "Government / Policy"},
    {"name": "Rory Linehan"},
    {"name": "Daniel Galle"},
    {"name": "Peter Rummel"},
    {"name": "Matt Gijselman"},
    {"name": "Amelia Burnett"},
    {"name": "Nathan Marsh", "context": "Region - EMEA"},
    {"name": "Allen Li", "context": "Region - APAC"},
    {"name": "Kaushik Chakraborty"},
    {"name": "Fabian Folgar", "context": "Region - NALA"},
    {"name": "Christoph Lorenz"},
    {"name": "Niklas Zeybrandt"},
    {"name": "Nick Niknam"},
    {"name": "Collin Ellam"},
    {"name": "Ken MacArthur"},
    {"name": "Angela Curry", "context": "CSR"},
    {"name": "Bernardo Matos", "context": "Government / Policy"},
    {"name": "David Lieberman"},
    {"name": "Andy Rahden", "context": "Product & Technology"},
    {"name": "Francois Valois", "context": "AEC"},
    {"name": "Oliver Conze", "context": "Bentley Infrastructure Cloud"},
    {"name": "Mike Schellhase", "context": "Energy / Transportation"},
    {"name": "Mark Pittman", "context": "Blyncsy / Roads"},
    {"name": "Lori Hufford"},
    {"name": "MK Sunil"},
    {"name": "Kannan Thiruvadi"},
    {"name": "Amit Shrivastava"},
    {"name": "Peng Du"},
    {"name": "Lydia Walpole"},
    {"name": "Heinz Hempert"},
    {"name": "Debu Chakraborty"},
    {"name": "Matt Sheridan"},
    {"name": "Pierre De Wet"},
    {"name": "Susanne Trierscheid"},
    {"name": "Molly Brown"},
    {"name": "Josh Taylor"},
    {"name": "Seth Guthrie"},
    {"name": "Geoff Mcdonald"},
    {"name": "Anatolii Ast"},
    {"name": "Amanda Morgan"},
    {"name": "Paul Connelly"},
    {"name": "Danny Williams"},
    {"name": "Shehzan Mohammed"},
    {"name": "Aaron Beazley"},
    {"name": "Greg Demchak", "context": "Design Vision & Innovation iLab"},
    {"name": "Dustin Parkman", "context": "Industry / AEC"},
    {"name": "Dave Philp"},
    {"name": "Hugh Hofmeister"},
    {"name": "Paul King"},
    {"name": "Vivek Kumar"},
    {"name": "Ashwin Nayak", "aliases": ["Aswin Nayak"]},
    {"name": "Richard Vestner", "context": "Cities / Water"},
    {"name": "Zubran Solaiman", "context": "Cities"},
    {"name": "Olly Thomas"},
    {"name": "Dorothea Manou"},
    {"name": "Marc Rietman"},
    {"name": "Jens Sauer"},
    {"name": "Gregg Herrin", "context": "Water", "aliases": ["Greg Herrin"]},
    {"name": "Slavco Velickov", "context": "Water"},
    {"name": "Dr. Tom Walski", "context": "Water", "aliases": ["Dr. Thom Krom"]},
    {"name": "Shar Govindan"},
    # Meltwater's tag is the misspelled "Cecila Correia" — that is the canonical
    # label to apply; the correct spelling stays as an alias for text detection.
    {"name": "Cecila Correia", "aliases": ["Cecelia Correia"]},
    {"name": "Amritanshu Kumar"},
    {"name": "Brad Johnson", "context": "Energy - Electric Utilities"},
    {"name": "Kevin Bates"},
    {"name": "Otto Lynch"},
    {"name": "Victoria Fillingham"},
    {"name": "Mark Biagi", "context": "Energy"},
    {"name": "Sharon Soler"},
    {"name": "Tony Turner"},
    {"name": "Bibhuti Aryal", "context": "Transportation"},
    {"name": "Dimple Patel"},
    {"name": "Alan Esguerra", "context": "Transportation"},
    {"name": "Burak Boyaci"},
    {"name": "Kasturi Srinivas"},
    {"name": "Andrew Smith"},
    {"name": "Leif Johnson"},
    {"name": "Graham Grant", "context": "Mining"},
    {"name": "Angela Harvey", "context": "Mining"},
    {"name": "Pat McLarin", "context": "Mining"},
    {"name": "Jeremy O'Brien", "context": "Mining"},
    {"name": "Janina Elliott", "context": "Mining"},
    {"name": "Kevin Hunt", "context": "Energy - Electric Utilities", "no_mw_tag": True},
    {"name": "Alan Ridgeway", "context": "Energy", "no_mw_tag": True},
    {"name": "Bill Panos", "context": "Roads", "no_mw_tag": True},
    {"name": "Bob Mankowski", "context": "Open Apps", "no_mw_tag": True},
    {"name": "Pavan Emani", "context": "iTwin Platform", "no_mw_tag": True},
    {"name": "Ken Adamson", "context": "iTwin Studio", "no_mw_tag": True},
    {"name": "Suzanne Little", "context": "HR / Colleague Success", "no_mw_tag": True},
    {"name": "Kristin Fallon", "context": "HR / Colleague Success", "no_mw_tag": True},
    # Present as a tag in Meltwater; was missing from the protocol-derived list.
    {"name": "Morgan Hays"},
]


# ---------------------------------------------------------------------------
# Convenience: flat views + the full canonical label set (for validation).
# ---------------------------------------------------------------------------
def spokesperson_label(name: str) -> str:
    # Meltwater uses a PIPE separator for spokespeople (e.g. "Spokesperson | Jim
    # Dobbs"), unlike the hyphen used by the other families.
    return f"Spokesperson | {name}"


def spokespeople_without_mw_tag() -> set[str]:
    """Spokesperson labels for people who exist in the protocol but have NO tag
    in Meltwater yet. Kept for text detection, but Phase-2 apply cannot apply
    them (there is nothing to check) — it must flag them for review instead."""
    return {spokesperson_label(sp["name"]) for sp in SPOKESPEOPLE if sp.get("no_mw_tag")}


ALL_TAG_GROUPS = {
    "type_of_publication": TYPE_OF_PUBLICATION,
    "type_of_coverage": TYPE_OF_COVERAGE,
    "region": REGION,
    "corporate": CORPORATE,
    "pillar": PILLAR,
    "industry": INDUSTRY,
    "product": PRODUCT,
}


def all_labels() -> set[str]:
    """Every canonical tag string the classifier is allowed to output."""
    labels: set[str] = set()
    for group in ALL_TAG_GROUPS.values():
        for tag in group:
            labels.add(tag["label"])
    for sp in SPOKESPEOPLE:
        labels.add(spokesperson_label(sp["name"]))
    return labels


def is_valid_label(label: str) -> bool:
    return label in all_labels()


def applicable_labels() -> set[str]:
    """Every Bentley tag that ACTUALLY EXISTS in Meltwater and may be applied in
    Phase 2 — i.e. all_labels() minus spokespeople who have no Meltwater tag yet.
    This is the allowlist Phase-2 apply should validate against so it never tries
    to apply a tag that isn't in the modal (and never touches another brand's
    tags like the Kaseya/Ninja sentiment tags that share the same account)."""
    return all_labels() - spokespeople_without_mw_tag()


# ---------------------------------------------------------------------------
# Deterministic name scans — Product and Spokesperson are LITERAL-NAME
# categories (protocol: "product tags require explicit names, never infer";
# spokespeople are named). So we can reliably find them by scanning the full
# article text, instead of relying on the model's recall.
# ---------------------------------------------------------------------------
def products_in_text(text: str) -> list[str]:
    low = (text or "").lower()
    out = []
    for p in PRODUCT:
        if any(a.lower() in low for a in p.get("aliases", [])):
            out.append(p["label"])
    return out


def spokespeople_in_text(text: str, require_bentley_context: bool = False,
                         window: int = 300) -> list[str]:
    """Spokesperson tags for people NAMED in the text.

    Many spokespeople have common names (Brad Johnson, James Lee, Andrew Smith,
    Molly Brown, Paul King …) that also belong to unrelated people at other
    companies — e.g. "Exacter appoints Brad Johnson" is NOT Bentley's Brad
    Johnson. With require_bentley_context=True, a name only counts when the word
    "Bentley" appears within `window` characters of an occurrence of that name,
    so a same-name person at another company is not tagged."""
    low = (text or "").lower()
    out = []
    for sp in SPOKESPEOPLE:
        names = [sp["name"]] + sp.get("aliases", [])
        hit = False
        for n in names:
            nlow = n.lower()
            if not require_bentley_context:
                if nlow in low:
                    hit = True
                    break
                continue
            # require "Bentley" near at least one occurrence of the name
            idx = low.find(nlow)
            while idx != -1:
                lo = max(0, idx - window)
                hi = idx + len(nlow) + window
                if "bentley" in low[lo:hi]:
                    hit = True
                    break
                idx = low.find(nlow, idx + 1)
            if hit:
                break
        if hit:
            out.append(spokesperson_label(sp["name"]))
    return out


def valid_spokesperson_labels() -> set[str]:
    return {spokesperson_label(sp["name"]) for sp in SPOKESPEOPLE}


# ---------------------------------------------------------------------------
# DYNAMIC OVERRIDES — let the client's protocol change WITHOUT a code edit.
#
# If brands/bentley/taxonomy_overrides.json exists (or $BENTLEY_TAXONOMY_OVERRIDES
# points at one), its add / remove / rename entries are applied to the lists
# above at import time. If the file is ABSENT or invalid, the hardcoded protocol
# is used UNCHANGED — so default behavior is exactly as before this was added.
#
# Format (every key optional):
# {
#   "add":    { "REGION":  [ {"label": "Region - LATAM", "keywords": ["latin america"]} ],
#               "PRODUCT": [ {"label": "New Product", "aliases": ["new product"]} ] },
#   "remove": { "PRODUCT": ["Old Product"] },
#   "rename": { "Region - EMEA": "Region - EMEA & Africa" },
#   "spokespeople_add":    [ {"name": "New Person", "aliases": ["N. Person"]} ],
#   "spokespeople_remove": [ "Former Person" ]
# }
# Family keys for add/remove: TYPE_OF_PUBLICATION, TYPE_OF_COVERAGE, REGION,
# CORPORATE, PILLAR, INDUSTRY, PRODUCT. rename matches any family label.
# ---------------------------------------------------------------------------
_FAMILY_LISTS = {
    "TYPE_OF_PUBLICATION": TYPE_OF_PUBLICATION,
    "TYPE_OF_COVERAGE": TYPE_OF_COVERAGE,
    "REGION": REGION,
    "CORPORATE": CORPORATE,
    "PILLAR": PILLAR,
    "INDUSTRY": INDUSTRY,
    "PRODUCT": PRODUCT,
}


def _apply_overrides() -> dict:
    """Apply optional external taxonomy overrides. Mutates the family lists +
    SPOKESPEOPLE IN PLACE (so ALL_TAG_GROUPS and every helper see the change).
    Returns a {added, removed, renamed} summary, or {} if nothing was applied.
    Never raises — a missing/invalid file leaves the hardcoded protocol intact."""
    import os
    import json
    path = os.environ.get("BENTLEY_TAXONOMY_OVERRIDES") or \
        os.path.join(os.path.dirname(__file__), "taxonomy_overrides.json")
    if not os.path.exists(path):
        return {}
    try:
        ov = json.load(open(path, encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(ov, dict):
        return {}
    summary = {"added": 0, "removed": 0, "renamed": 0}

    # ADD tag-family items
    for fam, items in (ov.get("add") or {}).items():
        lst = _FAMILY_LISTS.get(str(fam).upper())
        if lst is None or not isinstance(items, list):
            continue
        have = {t.get("label") for t in lst}
        for it in items:
            if isinstance(it, dict) and it.get("label") and it["label"] not in have:
                lst.append(it)
                have.add(it["label"])
                summary["added"] += 1

    # REMOVE tag-family items by label
    for fam, labels in (ov.get("remove") or {}).items():
        lst = _FAMILY_LISTS.get(str(fam).upper())
        if lst is None or not isinstance(labels, list):
            continue
        drop = set(labels)
        kept = [t for t in lst if t.get("label") not in drop]
        summary["removed"] += len(lst) - len(kept)
        lst[:] = kept                       # in-place so ALL_TAG_GROUPS stays valid

    # RENAME labels across all families
    ren = ov.get("rename") or {}
    if isinstance(ren, dict):
        for lst in _FAMILY_LISTS.values():
            for t in lst:
                if t.get("label") in ren:
                    t["label"] = ren[t["label"]]
                    summary["renamed"] += 1

    # SPOKESPEOPLE add / remove
    have_sp = {sp.get("name") for sp in SPOKESPEOPLE}
    for sp in (ov.get("spokespeople_add") or []):
        if isinstance(sp, dict) and sp.get("name") and sp["name"] not in have_sp:
            SPOKESPEOPLE.append(sp)
            have_sp.add(sp["name"])
            summary["added"] += 1
    sp_rm = set(ov.get("spokespeople_remove") or [])
    if sp_rm:
        kept = [sp for sp in SPOKESPEOPLE if sp.get("name") not in sp_rm]
        summary["removed"] += len(SPOKESPEOPLE) - len(kept)
        SPOKESPEOPLE[:] = kept

    return summary if any(summary.values()) else {}


# Applied once, at import — safe no-op when no overrides file is present.
OVERRIDES_APPLIED = _apply_overrides()
