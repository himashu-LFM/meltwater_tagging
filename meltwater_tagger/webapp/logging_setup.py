"""
Central logging setup. Import `get_logger(__name__)` from any module instead
of calling logging.getLogger directly, so every module shares the same format
and handler config.

Output goes to stdout — visible in your local terminal, and captured by
Render's log viewer (Dashboard -> your service -> Logs) for the deployed app.

Set MELTWATER_LOG_LEVEL=DEBUG in .env for verbose per-post tracing (fetch
attempts, classification calls, card-by-card apply actions). Default is INFO.
"""

import logging
import os
import sys

_LEVEL = os.environ.get("MELTWATER_LOG_LEVEL", "INFO").upper()

_configured = False


def _configure_root():
    global _configured
    if _configured:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-7s [%(name)s] %(message)s", datefmt="%H:%M:%S"
    ))
    root = logging.getLogger()
    root.setLevel(_LEVEL)
    root.addHandler(handler)
    _configured = True


def get_logger(name: str) -> logging.Logger:
    _configure_root()
    return logging.getLogger(name)
