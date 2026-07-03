# Meltwater Sentiment Tagger (script version)

A faster, scripted replacement for the `meltwater-sentiment-tagger` skill.
Same logic, same safety rules — but the slow parts are removed:

| Skill (slow)                                            | Script (fast)                                              |
|---------------------------------------------------------|-----------------------------------------------------------|
| Reads + judges posts one at a time, LLM in the browser  | **Phase 1** fetches full text + classifies posts *in parallel*, no browser |
| Tags one post at a time with the LLM thinking each step | **Phase 2** mechanically applies the *precomputed* tags — no LLM in the loop |

Tagging still happens in the Meltwater UI (the tool has no tag-upload API), so
Phase 2 drives the browser — but it only performs fast UI actions from a
ready-made decision list.

## Why two phases?
- **Phase 1 — `classify.py`**: reads a Meltwater Excel export, fetches each
  post's full text (Reddit `.json`), and asks Claude to judge sentiment using
  the skill's exact rules (attribution certainty, sarcasm, primary-brand
  selection, Kaseya roll-up). Output: `decisions.json`.
- **Phase 2 — `apply_tags.py`**: opens Meltwater, walks the topic feed, and for
  each *untagged* post applies the precomputed tag via the documented UI flow
  (hover → tag icon → "Tag content" → Find → check → Apply). Output: `report.md`.

## Setup
```bash
pip install -r requirements.txt
python -m playwright install chromium
export ANTHROPIC_API_KEY=sk-ant-...        # PowerShell: $env:ANTHROPIC_API_KEY="..."
```

## Run
```bash
# 1. Export the topic results from Meltwater to .xlsx (include the post URL column).
# 2. Classify (parallel, fast):
python classify.py "Kaseya V2 export.xlsx"          # brand inferred from the topic column
#    or force it:  python classify.py export.xlsx --brand Kaseya

# 3. Review decisions.json (optional but recommended).

# 4. Dry run the apply pass first — walks the feed, applies nothing, writes report:
python apply_tags.py --dry-run

# 5. Apply for real:
python apply_tags.py
```
The first apply run opens a browser with a persistent profile — log into
Meltwater, open the topic feed, and press Enter. Your login is reused next time.

## Safety rules carried over from the skill
- Skips any post already carrying a `Remove [sentiment-tag]` chip (never
  re-judges or overrides existing tags).
- Expands each "Similar" group exactly once (state detected via `aria-label`).
- Never creates tags, exports, or changes account/search settings.
- Posts judged `review` / `skip_flag` (another brand) / `paywall` are **flagged
  in the report, never tagged** — matching the skill's report format.

## Web UI (upload / paste URLs → classify → export)
A browser UI for the classification step, with Excel export.

```bash
pip install -r requirements.txt
python webapp/app.py
# open http://127.0.0.1:5000
```
- **Upload** a Meltwater export (only the URL column is used) **or paste** one or
  more post URLs.
- Set the **run brand** (auto-filled from the export's topic when available) and
  the **fetch mode** (Real Chrome / CDP recommended — start Chrome with the debug
  port first, see Option B below).
- Runs the same pipeline as `classify.py`, shows a results table, and the
  **Export Excel** button downloads the tagged post URLs.

Needs `ANTHROPIC_API_KEY` in `.env` (same as the CLI). Applying tags into
Meltwater is still done by `apply_tags.py`.

## Reddit full-text (important for Reddit feeds)
The skill's accuracy comes from reading each post's **full text**. Meltwater
exports often don't include the post body (Title/Opening Text/Hit Sentence can be
empty), so the script fetches it from the permalink. Reddit **rate-limits
anonymous access** (HTTP 429), so:

- By default the script fetches anonymously with throttling + retries
  (`MELTWATER_FETCH_CONCURRENCY=2`, `MELTWATER_REDDIT_MIN_INTERVAL=1.5`). On a
  home/office IP this often works; raise the interval if you still see 429s.
- **Recommended for Reddit-heavy feeds — use the free Reddit API:**
  1. Go to https://www.reddit.com/prefs/apps → "create another app" → type
     **script**. Name anything; redirect URI `http://localhost`.
  2. Copy the **client ID** (under the app name) and **secret**.
  3. Add to your `.env`:
     ```
     REDDIT_CLIENT_ID=your_id
     REDDIT_CLIENT_SECRET=your_secret
     REDDIT_USER_AGENT=windows:meltwater-tagger:1.0 (by /u/your_username)
     ```
  The script then uses `oauth.reddit.com` (reliable, ~100 requests/min).

After `classify.py` runs it prints `Full text fetched for N/M posts`. If N is
low, fix the fetch (add Reddit creds) before trusting the classifications.

### Option B — attach to your real Chrome (no API key)
Reddit blocks Playwright-launched browsers (bot wall), but not your own Chrome.
Start Chrome yourself with a debug port, log into Reddit there, then let the
script attach to it.

1. **Fully quit Chrome** (check Task Manager — no `chrome.exe` running).
2. Start Chrome with a debug port and a dedicated profile (one line, PowerShell):
   ```powershell
   & "C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --user-data-dir="C:\mw-chrome-profile"
   ```
3. In that Chrome window, **log into reddit.com** (first time only — the profile
   at `C:\mw-chrome-profile` remembers it).
4. Leave that Chrome open and run:
   ```powershell
   python classify.py "Kaseya_V2_Reddit_Test.xlsx" --brand Kaseya --cdp
   ```
   The script attaches to that Chrome and fetches through your trusted session.

Override the port/URL with `MELTWATER_CHROME_CDP_URL` if you use a different one.

## Config (env vars, see `config.py`)
- `MELTWATER_MODEL` — default `claude-opus-4-8`; set `claude-sonnet-4-6` for a
  cheaper/faster classification run.
- `MELTWATER_CLASSIFY_CONCURRENCY`, `MELTWATER_FETCH_CONCURRENCY` — parallelism.
- `MELTWATER_HEADLESS` — `true` to run the apply browser headless (after first login).

## The one thing you may need to tune
Meltwater's DOM isn't public, so the CSS/aria selectors in `apply_tags.py`
(`SELECTORS` block) are built from the labels documented in the skill
(`Remove [tag]`, `Open similar articles`, `Tag content`, `Find`, `Apply`,
`Open article in new tab`). If a selector misses on your account, open the
browser with `--dry-run`, inspect the card DOM, and adjust that one block — the
overall flow stays the same.
