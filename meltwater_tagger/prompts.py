"""
Backward-compatibility shim.

The Kaseya prompts moved to ``brands/kaseya/prompts.py`` as part of the
multi-brand refactor. Existing code (``classify.py``, ``webapp/classify_web.py``)
imports ``prompts`` at the top level, so this module re-exports the Kaseya
prompts unchanged. New code should import from the brand profile instead
(see ``brands/__init__.py``: ``get_profile("Kaseya")``).
"""

from brands.kaseya.prompts import *  # noqa: F401,F403
from brands.kaseya.prompts import (  # explicit re-exports used across the app
    SYSTEM_PROMPT,
    POST_TEMPLATE,
    COMMENT_TEMPLATE,
    CONTENT_TYPE_GUIDANCE,
    DECISION_SCHEMA,
)
