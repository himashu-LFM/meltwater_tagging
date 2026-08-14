"""
Backward-compatibility shim.

The Kaseya taxonomy moved to ``brands/kaseya/taxonomy.py`` as part of the
multi-brand refactor. Existing code (``classify.py``, ``webapp/classify_web.py``)
imports ``taxonomy`` at the top level, so this module re-exports the Kaseya
taxonomy unchanged. New code should import from the brand profile instead
(see ``brands/__init__.py``: ``get_profile("Kaseya")``).
"""

from brands.kaseya.taxonomy import *  # noqa: F401,F403
from brands.kaseya.taxonomy import (  # explicit re-exports used across the app
    AVAILABLE_BRANDS,
    SENTIMENTS,
    KASEYA_FAMILY,
    normalize_brand,
    tag_name,
    is_valid_tag,
)
