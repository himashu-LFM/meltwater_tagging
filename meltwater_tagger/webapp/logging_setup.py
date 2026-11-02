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
import re
import sys

_LEVEL = os.environ.get("MELTWATER_LOG_LEVEL", "INFO").upper()

_configured = False

# Every Supabase read/write logs an httpx line like
#   INFO [httpx] HTTP Request: GET https://...supabase.co/rest/v1/... "HTTP/2 200 OK"
# There are dozens per run and they drown the lines that actually say what the
# tagger is doing. Drop them while they are SUCCESSFUL; anything that is not a
# 2xx/3xx still comes through, as does every warning and error.
_OK_STATUS = re.compile(r'"HTTP/[\d.]+ [23]\d\d')


class _QuietSupabase(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno > logging.INFO or record.name != "httpx":
            return True
        msg = record.getMessage()
        if "supabase.co" not in msg:
            return True
        return not _OK_STATUS.search(msg)      # keep failures, hide successes


def _configure_root():
    global _configured
    if _configured:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-7s [%(name)s] %(message)s", datefmt="%H:%M:%S"
    ))
    handler.addFilter(_QuietSupabase())
    root = logging.getLogger()
    root.setLevel(_LEVEL)
    root.addHandler(handler)
    _configured = True


def get_logger(name: str) -> logging.Logger:
    _configure_root()
    return logging.getLogger(name)
