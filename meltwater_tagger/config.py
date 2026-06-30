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

# Model used to judge sentiment. The skill's nuance (sarcasm, attribution
# certainty, primary-brand selection) is demanding, so default to Opus.
# Override with MELTWATER_MODEL=claude-sonnet-4-6 for a cheaper/faster run.
MODEL = os.environ.get("MELTWATER_MODEL", "claude-opus-4-8")

# How many posts to classify concurrently (Claude API calls in flight).
CLASSIFY_CONCURRENCY = int(os.environ.get("MELTWATER_CLASSIFY_CONCURRENCY", "8"))

# How many post permalinks to fetch full text for concurrently.
# Reddit rate-limits anonymous access hard; keep this low (2-3) unless you use
# the Reddit API (credentials below), which tolerates more.
FETCH_CONCURRENCY = int(os.environ.get("MELTWATER_FETCH_CONCURRENCY", "2"))

# Minimum seconds between Reddit requests (anonymous). Raise if you still see 429s.
REDDIT_MIN_INTERVAL = float(os.environ.get("MELTWATER_REDDIT_MIN_INTERVAL", "1.5"))

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
# Option B: attach to a real Chrome you start with --remote-debugging-port.
# This avoids Playwright's automation fingerprint that Reddit blocks.
CHROME_CDP_URL = os.environ.get("MELTWATER_CHROME_CDP_URL", "http://localhost:9222")

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
