"""Central configuration for the Meltwater sentiment tagger."""

import os


def _load_dotenv():
    """Load KEY=VALUE lines from a local .env into os.environ (no dependency).

    Looks in this folder and the parent. Existing real env vars win.
    """
    here = os.path.dirname(__file__)
    for path in (os.path.join(here, ".env"), os.path.join(here, "..", ".env")):
        if not os.path.isfile(path):
            continue
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key, val = key.strip(), val.strip().strip('"').strip("'")
                os.environ.setdefault(key, val)


_load_dotenv()

# --- Classification (phase 1) ---------------------------------------------

# Model used to judge sentiment. Default to Sonnet for cost; override with
# MELTWATER_MODEL=claude-opus-4-8 if you need the extra nuance on a hard batch.
MODEL = os.environ.get("MELTWATER_MODEL", "claude-sonnet-4-6")

# How many posts to classify concurrently (Claude API calls in flight).
CLASSIFY_CONCURRENCY = int(os.environ.get("MELTWATER_CLASSIFY_CONCURRENCY", "8"))

# How many post permalinks to fetch full text for concurrently.
# Reddit rate-limits anonymous access hard; keep this low (2-3) unless you use
# the Reddit API (credentials below), which tolerates more.
FETCH_CONCURRENCY = int(os.environ.get("MELTWATER_FETCH_CONCURRENCY", "2"))

# Minimum seconds between Reddit requests (anonymous). Raise if you still see 429s.
# Seconds between Reddit requests (global, serialized). Reddit rate-limits the
# public RSS feed per IP fairly aggressively — bursts of ~6-8 requests start
# returning 429/403 and the budget then needs a few minutes to recover. 3s keeps
# a long batch inside the budget; lower it only if you have API credentials.
REDDIT_MIN_INTERVAL = float(os.environ.get("MELTWATER_REDDIT_MIN_INTERVAL", "3.0"))

# Optional Reddit official API (recommended for Reddit-heavy feeds).
# Create a free "script" app at https://www.reddit.com/prefs/apps and set:
#   REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET  (in your .env)
# When both are present, the script uses oauth.reddit.com (reliable, higher limit).
REDDIT_CLIENT_ID = os.environ.get("REDDIT_CLIENT_ID", "")
REDDIT_CLIENT_SECRET = os.environ.get("REDDIT_CLIENT_SECRET", "")
REDDIT_USER_AGENT = os.environ.get(
    "REDDIT_USER_AGENT",
    "windows:meltwater-sentiment-tagger:1.0 (by /u/your_reddit_username)",
)

# --- Apify Reddit scraper (paid, fastest) ---------------------------------
# Reddit meters anonymous access at ~1 request per 60s per IP, so a 100-mention
# batch takes ~40 min. Apify's actor returns one record per URL (post OR the
# specific comment) with no rate limit: measured 36 mentions in 10s.
# Set APIFY_TOKEN (Apify Console -> Settings -> API & Integrations) to enable.
APIFY_TOKEN = os.environ.get("APIFY_TOKEN", "")
# Actor slug (username~actor-name). Verified against real Kaseya data.
APIFY_ACTOR = os.environ.get("APIFY_ACTOR", "fatihtahta~reddit-scraper-search-fast")
# How long to wait for one synchronous actor run (seconds).
APIFY_TIMEOUT = int(os.environ.get("APIFY_TIMEOUT", "300"))
# Mentions per actor run. The actor handles large batches fine; chunking keeps
# any single run well inside the sync timeout.
APIFY_BATCH_SIZE = int(os.environ.get("APIFY_BATCH_SIZE", "200"))
# Comment cap for the parent-thread retry used when the actor's direct
# comment-permalink lookup returns nothing. Every comment returned is billed, so
# this is a ceiling on the cost of recovering one missed mention.
APIFY_THREAD_MAX_COMMENTS = int(os.environ.get("APIFY_THREAD_MAX_COMMENTS", "500"))
# Bulk OAuth fetch: requests/minute budget and how many calls may be in flight.
# Reddit's documented budget is ~100 rpm per client id; stay just under it.
# Concurrency only hides latency — the token bucket is what caps the rate.
REDDIT_RPM = float(os.environ.get("MELTWATER_REDDIT_RPM", "95"))
REDDIT_BULK_CONCURRENCY = int(os.environ.get("MELTWATER_REDDIT_BULK_CONCURRENCY", "8"))
# Option B: attach to a real Chrome you start with --remote-debugging-port.
# This avoids Playwright's automation fingerprint that Reddit blocks.
# Use 127.0.0.1 (IPv4), not "localhost" — Chrome's debug port listens on IPv4 and
# "localhost" can resolve to IPv6 (::1), causing ECONNREFUSED.
CHROME_CDP_URL = os.environ.get("MELTWATER_CHROME_CDP_URL", "http://127.0.0.1:9222")

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# Max characters of full post text to send to the model per post.
MAX_POST_CHARS = int(os.environ.get("MELTWATER_MAX_POST_CHARS", "12000"))

# --- Apply (phase 2) ------------------------------------------------------

# Meltwater base URL.
MELTWATER_URL = os.environ.get("MELTWATER_URL", "https://app.meltwater.com")

# Where Playwright stores the logged-in browser profile so you only log in once.
USER_DATA_DIR = os.environ.get(
    "MELTWATER_USER_DATA_DIR",
    os.path.join(os.path.dirname(__file__), ".mw_browser_profile"),
)

# Run the browser headless during apply. Default False so you can watch / log in.
HEADLESS = os.environ.get("MELTWATER_HEADLESS", "false").lower() == "true"

# Pause (ms) between UI actions, to stay gentle on the app.
ACTION_DELAY_MS = int(os.environ.get("MELTWATER_ACTION_DELAY_MS", "400"))

# --- Files ----------------------------------------------------------------

DECISIONS_FILE = os.environ.get("MELTWATER_DECISIONS_FILE", "decisions.json")
REPORT_FILE = os.environ.get("MELTWATER_REPORT_FILE", "report.md")

# --- Outbound email (welcome + password-reset code) -----------------------
# Standard SMTP; works with Gmail/Google Workspace (smtp.gmail.com:587 + an App
# Password), SendGrid, etc. If SMTP_HOST is unset, email features no-op (and log
# a warning) instead of crashing.
SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
# Friendly From header; falls back to SMTP_USER if not set.
SMTP_FROM = os.environ.get("SMTP_FROM", "") or SMTP_USER
# Public URL of the app, used in email copy / links.
APP_BASE_URL = os.environ.get("APP_BASE_URL", "https://listeningmw.listenfirst.in")
