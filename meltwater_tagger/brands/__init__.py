"""
Brand registry — the seam that lets one shared engine serve many brands.

Each brand lives in its own folder (``brands/kaseya/``, ``brands/bentley/``)
and exposes the pieces the engine needs. ``get_profile(name)`` returns a small
object bundling those pieces so callers never import a specific brand directly.

    from brands import get_profile
    profile = get_profile("Kaseya")   # or "Bentley"

This is intentionally thin for now. Kaseya keeps working through the top-level
``taxonomy.py`` / ``prompts.py`` shims; wiring ``classify.py`` to consume
``get_profile()`` is a later engine step (Track B). Adding a third brand is a
new folder here — no engine change.
"""

from types import SimpleNamespace


_ALIASES = {
    "kaseya": "kaseya",
    "kaseya v2": "kaseya",
    "kaseya 365": "kaseya",
    "ninja": "kaseya",          # Ninja rides the same sentiment pipeline as Kaseya
    "bentley": "bentley",
}


def normalize_brand_name(name: str) -> str:
    """Map a free-text brand name to a profile key ('kaseya' | 'bentley')."""
    if not name:
        raise ValueError("brand name is required")
    key = _ALIASES.get(name.strip().lower())
    if key:
        return key
    # default: any unknown sentiment-style brand uses the Kaseya profile
    return "kaseya"


def get_profile(name: str) -> SimpleNamespace:
    """Return the brand profile bundle for ``name``.

    Bentley uses the taxonomy-tagging pipeline (many tags per item, scope gate);
    Kaseya/Ninja use the sentiment pipeline (one tag per item). Only the pieces
    that already exist are attached — the rest fill in as they are built.
    """
    key = normalize_brand_name(name)

    if key == "bentley":
        from brands.bentley import taxonomy as bentley_taxonomy
        return SimpleNamespace(
            key="bentley",
            run_brand=bentley_taxonomy.RUN_BRAND,
            style="taxonomy",           # many tags per item + scope gate
            taxonomy=bentley_taxonomy,
            # prompts / schema / fetcher / rules attach here as they are built
        )

    # kaseya (also the fallback for other sentiment brands)
    from brands.kaseya import taxonomy as kaseya_taxonomy
    from brands.kaseya import prompts as kaseya_prompts
    return SimpleNamespace(
        key="kaseya",
        run_brand="Kaseya",
        style="sentiment",              # one sentiment tag per item
        taxonomy=kaseya_taxonomy,
        prompts=kaseya_prompts,
    )
