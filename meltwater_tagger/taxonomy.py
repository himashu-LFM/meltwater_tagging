"""
Tag taxonomy and brand roll-up rules.

Mirrors the "TAG TAXONOMY REFERENCE" section of the SKILL.md so the script
applies exactly the tags the skill would, and never invents new ones.
"""

# Brands that have Positive / Negative / Neutral - <Brand> tags in the account.
AVAILABLE_BRANDS = [
    "Kaseya",
    "ConnectWise",
    "HaloPSA",
    "Huntress",
    "Ninja",
    "Pax8",
    "Syncro",
    "Veeam",
    "N-Able",
]

SENTIMENTS = ["Positive", "Negative", "Neutral"]

# Kaseya family roll-up: any of these products -> brand "Kaseya".
# (lower-cased substrings, matched against the product/brand mention)
KASEYA_FAMILY = [
    "kaseya",
    "datto",  # covers Datto EDR/AV/RMM/BCDR/Siris/Alto
    "it glue",
    "itglue",
    "autotask",
    "unitrends",
    "rocketcyber",
    "graphus",
    "id agent",
    "pulseway",
    "saas alerts",
    "backupify",
    "bullphish",
    "vonahi",
]


def normalize_brand(raw_brand: str) -> str | None:
    """
    Map a raw brand/product mention to the canonical run-brand it rolls up to.
    Returns None if it does not match any taggable brand.
    """
    if not raw_brand:
        return None
    b = raw_brand.strip().lower()

    # Kaseya family roll-up
    for token in KASEYA_FAMILY:
        if token in b:
            return "Kaseya"

    # Direct brand match (handle a couple of spelling variants)
    aliases = {
        "n-able": "N-Able",
        "nable": "N-Able",
        "n able": "N-Able",
        "connectwise": "ConnectWise",
        "connect wise": "ConnectWise",
        "halopsa": "HaloPSA",
        "halo psa": "HaloPSA",
        "huntress": "Huntress",
        "ninja": "Ninja",
        "ninjaone": "Ninja",
        "pax8": "Pax8",
        "syncro": "Syncro",
        "veeam": "Veeam",
    }
    if b in aliases:
        return aliases[b]
    for key, canonical in aliases.items():
        if key in b:
            return canonical
    return None


def tag_name(sentiment: str, brand: str) -> str:
    """
    Build the exact tag string as it appears in Meltwater.

    Account convention (confirmed with Ritu): brand first, lowercase sentiment,
    spaces around the dash -> 'Kaseya - positive', 'Ninja - negative'.
    Phase 2 matches this string character-for-character against the tag in the
    Meltwater modal, so the format must match the account's tags exactly.
    """
    return f"{brand} - {sentiment.lower()}"


def is_valid_tag(sentiment: str, brand: str) -> bool:
    return sentiment in SENTIMENTS and brand in AVAILABLE_BRANDS
