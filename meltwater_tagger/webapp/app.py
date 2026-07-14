"""
Web UI for the Meltwater sentiment tagger — multi-user, multi-brand.

Run:
    python webapp/app.py
Then open http://127.0.0.1:5000

Auth + per-user Meltwater/Reddit credentials + run history are backed by
Supabase (see supabase/schema.sql and .env.example). Classification reuses
the same pipeline as classify.py, so tagging logic is identical everywhere.
"""

import asyncio
import io
import os
import sys

import pandas as pd
from flask import Flask, jsonify, request, send_file, render_template, g
from anthropic import AsyncAnthropic, AuthenticationError, APIStatusError

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_DIR = os.path.dirname(_THIS_DIR)
# Under gunicorn (module import "webapp.app:app"), webapp/'s own folder is NOT
# auto-added to sys.path the way it is when running "python webapp/app.py"
# directly — so bare imports like "import db" would fail. Add both explicitly.
sys.path.insert(0, _PROJECT_DIR)
sys.path.insert(0, _THIS_DIR)

import config
from classify import (
    fetch_full_text, fetch_and_enrich, fetch_via_cdp, classify_post, _find_col,
    PERMALINK_HINTS, infer_brand, TOPIC_HINTS,
)
import httpx

import db
from auth import require_auth
from fetchers import fetch_via_reddit_cookie
from meltwater_apply import apply_results_to_meltwater, apply_via_session, decode_session_expiry
import classify_web
from logging_setup import get_logger

log = get_logger("app")

app = Flask(__name__, static_folder="static", template_folder="templates")

# CDP fetch needs a local logged-in Chrome, which a cloud host doesn't have.
ALLOW_CDP = os.environ.get("MELTWATER_ALLOW_CDP", "true").lower() == "true"


def run_async(coro):
    """Run an async coroutine to completion from Flask's sync context.

    On Windows, Playwright's subprocess transport can emit a harmless
    'RuntimeError: Event loop is closed' from its __del__ during teardown, AFTER
    the work has already finished successfully. It doesn't affect results but it
    looks alarming in the logs, so we install an exception handler that swallows
    exactly that message and lets everything else through unchanged."""
    loop = asyncio.new_event_loop()

    def _ignore_closed(lp, context):
        exc = context.get("exception")
        # Check BOTH the context message and the exception text. httpx/anyio can
        # tear a connection down after this per-request loop is already closed,
        # surfacing as a GC'd task whose message is "Task exception was never
        # retrieved" but whose exception is RuntimeError('Event loop is closed').
        msg = context.get("message", "") or ""
        exc_str = str(exc) if exc else ""
        if "Event loop is closed" in msg or "Event loop is closed" in exc_str:
            return  # benign async teardown noise
        lp.default_exception_handler(context)

    loop.set_exception_handler(_ignore_closed)
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        except Exception:
            pass
        asyncio.set_event_loop(None)
        loop.close()


# --- pages -------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html", allow_cdp=ALLOW_CDP,
                            supabase_url=db.SUPABASE_URL, supabase_anon_key=db.SUPABASE_ANON_KEY)


@app.route("/login")
def login_page():
    return render_template("login.html", supabase_url=db.SUPABASE_URL, supabase_anon_key=db.SUPABASE_ANON_KEY)


@app.route("/profile")
def profile_page():
    return render_template("profile.html", supabase_url=db.SUPABASE_URL, supabase_anon_key=db.SUPABASE_ANON_KEY)


@app.route("/history")
def history_page():
    return render_template("history.html", supabase_url=db.SUPABASE_URL, supabase_anon_key=db.SUPABASE_ANON_KEY)


@app.route("/brands")
def brands_page():
    return render_template("brands.html", supabase_url=db.SUPABASE_URL, supabase_anon_key=db.SUPABASE_ANON_KEY)


# --- brands --------------------------------------------------------------

@app.route("/api/brands", methods=["GET"])
@require_auth
def get_brands():
    try:
        brands = db.list_brands()
        log.info("listed %d brands for user=%s", len(brands), g.user.id)
        return jsonify({"brands": brands})
    except Exception as e:
        log.exception("GET /api/brands failed for user=%s", g.user.id)
        return jsonify({"error": str(e)}), 500


@app.route("/api/brands", methods=["POST"])
@require_auth
def upsert_brand_route():
    data = request.get_json(force=True)
    name = (data.get("name") or "").strip()
    if not name:
        log.warning("POST /api/brands rejected: missing name (user=%s)", g.user.id)
        return jsonify({"error": "Brand name is required"}), 400
    try:
        brand = db.upsert_brand(
            name,
            roll_up_terms=data.get("roll_up_terms"),
            meltwater_topic_url=data.get("meltwater_topic_url"),
        )
        log.info("brand upserted: name=%r id=%s (user=%s)", name, brand.get("id"), g.user.id)
        return jsonify({"brand": brand})
    except Exception as e:
        log.exception("POST /api/brands failed: name=%r (user=%s)", name, g.user.id)
        return jsonify({"error": str(e)}), 500


@app.route("/api/brands/<int:brand_id>", methods=["PUT"])
@require_auth
def update_brand_route(brand_id):
    data = request.get_json(force=True)
    name = data.get("name")
    if name is not None and not name.strip():
        return jsonify({"error": "Brand name cannot be empty"}), 400
    brand = db.update_brand(
        brand_id,
        name=name.strip() if name else None,
        roll_up_terms=data.get("roll_up_terms"),
        meltwater_topic_url=data.get("meltwater_topic_url"),
    )
    return jsonify({"brand": brand})


@app.route("/api/brands/<int:brand_id>", methods=["DELETE"])
@require_auth
def delete_brand_route(brand_id):
    try:
        db.delete_brand(brand_id)
        log.info("brand deleted: id=%s (user=%s)", brand_id, g.user.id)
        return jsonify({"ok": True})
    except Exception as e:
        log.exception("DELETE /api/brands/%s failed (user=%s)", brand_id, g.user.id)
        return jsonify({"error": str(e)}), 500


@app.route("/api/brands/<int:brand_id>/my-topic-url", methods=["GET"])
@require_auth
def get_my_topic_url(brand_id):
    return jsonify({"topic_url": db.get_user_topic_url(g.user.id, brand_id)})


@app.route("/api/brands/<int:brand_id>/my-topic-url", methods=["POST"])
@require_auth
def set_my_topic_url(brand_id):
    data = request.get_json(force=True)
    topic_url = (data.get("topic_url") or "").strip()
    if not topic_url:
        return jsonify({"error": "Topic URL is required"}), 400
    db.upsert_user_topic_url(g.user.id, brand_id, topic_url)
    return jsonify({"ok": True})


@app.route("/api/brands/<int:brand_id>/tags", methods=["GET"])
@require_auth
def get_brand_tags_route(brand_id):
    return jsonify({"tags": db.get_brand_tags(brand_id)})


@app.route("/api/brands/<int:brand_id>/tags", methods=["POST"])
@require_auth
def save_brand_tags_route(brand_id):
    """Save the three sentiment tag labels + rules for a brand in one call."""
    data = request.get_json(force=True)
    tags = data.get("tags", [])
    for t in tags:
        sentiment = (t.get("sentiment") or "").lower()
        if sentiment not in ("positive", "negative", "neutral"):
            continue
        label = (t.get("tag_label") or "").strip()
        rule = (t.get("rule") or "").strip() or None
        if not label:
            continue  # a tag must have a label
        db.upsert_brand_tag(brand_id, sentiment, label, rule)
    return jsonify({"ok": True})


# --- profile: meltwater + reddit creds ---------------------------------------

@app.route("/api/profile/meltwater", methods=["GET"])
@require_auth
def get_meltwater_profile():
    creds = db.get_meltwater_creds(g.user.id)
    return jsonify({"credentials": creds})


@app.route("/api/profile/meltwater", methods=["POST"])
@require_auth
def set_meltwater_profile():
    data = request.get_json(force=True)
    email = (data.get("email") or "").strip()
    password = data.get("password") or None
    if not email:
        log.warning("POST /api/profile/meltwater rejected: missing email (user=%s)", g.user.id)
        return jsonify({"error": "Meltwater email is required"}), 400
    try:
        db.upsert_meltwater_creds(g.user.id, email, password)
        # never log the password value itself
        log.info("Meltwater creds saved for user=%s (password_changed=%s)", g.user.id, bool(password))
        return jsonify({"ok": True})
    except Exception as e:
        log.exception("POST /api/profile/meltwater failed (user=%s)", g.user.id)
        return jsonify({"error": str(e)}), 500


@app.route("/api/profile/meltwater-session", methods=["GET"])
@require_auth
def get_meltwater_session_profile():
    meta = db.get_meltwater_session_meta(g.user.id)
    return jsonify({"session": meta})


@app.route("/api/profile/meltwater-session", methods=["POST"])
@require_auth
def set_meltwater_session_profile():
    data = request.get_json(force=True)
    value = (data.get("storage_value") or "").strip()
    if not value:
        log.warning("POST /api/profile/meltwater-session rejected: empty value (user=%s)", g.user.id)
        return jsonify({"error": "Paste the Local Storage value first"}), 400

    # A truncated copy (Chrome's DevTools grid shows a shortened preview by
    # default) is invalid JSON and would silently break injection later --
    # catch it here with an actionable message instead.
    import json as _json
    try:
        parsed = _json.loads(value)
    except Exception:
        log.warning("POST /api/profile/meltwater-session rejected: not valid JSON, "
                     "likely a truncated copy (user=%s, len=%d)", g.user.id, len(value))
        return jsonify({"error": (
            f"That doesn't look like the complete value (got {len(value)} characters, but it's not "
            "valid JSON — it's been cut off somewhere). Chrome's Local Storage grid and its "
            "right-click 'Copy value' can both truncate very long values. Use the Console command "
            "on the Profile page instead — it copies the exact full value with no truncation."
        )}), 400
    if not (isinstance(parsed, dict) and parsed.get("body", {}).get("access_token")):
        log.warning("POST /api/profile/meltwater-session rejected: missing access_token (user=%s)", g.user.id)
        return jsonify({"error": "That value parsed as JSON but doesn't contain an access_token — "
                                  "make sure you copied the @@auth0spajs@@ row, not a different key."}), 400

    exp = decode_session_expiry(value)
    try:
        db.upsert_meltwater_session(g.user.id, value)
        log.info("Meltwater session saved for user=%s, expires=%s", g.user.id, exp)
        return jsonify({"ok": True, "expires_at": exp})
    except Exception as e:
        log.exception("POST /api/profile/meltwater-session failed (user=%s)", g.user.id)
        return jsonify({"error": str(e)}), 500


@app.route("/api/profile/meltwater-session/status", methods=["GET"])
@require_auth
def meltwater_session_status():
    value = db.get_meltwater_session(g.user.id) if db.is_configured() else None
    if not value:
        return jsonify({"state": "none"})
    exp = decode_session_expiry(value)
    if not exp:
        return jsonify({"state": "unknown"})
    import time
    remaining = exp - time.time()
    if remaining <= 0:
        return jsonify({"state": "expired", "expires_at": exp})
    return jsonify({"state": "active", "expires_at": exp, "seconds_remaining": int(remaining)})


@app.route("/api/profile/reddit", methods=["GET"])
@require_auth
def get_reddit_profile():
    session = db.get_reddit_session(g.user.id)
    return jsonify({"session": session})


@app.route("/api/profile/reddit", methods=["POST"])
@require_auth
def set_reddit_profile():
    data = request.get_json(force=True)
    cookie = (data.get("cookie_value") or "").strip()
    if not cookie:
        log.warning("POST /api/profile/reddit rejected: missing cookie (user=%s)", g.user.id)
        return jsonify({"error": "Cookie value is required"}), 400
    try:
        db.upsert_reddit_cookie(g.user.id, cookie)
        # never log the cookie value itself
        log.info("Reddit session cookie saved for user=%s", g.user.id)
        return jsonify({"ok": True})
    except Exception as e:
        log.exception("POST /api/profile/reddit failed (user=%s)", g.user.id)
        return jsonify({"error": str(e)}), 500


@app.route("/api/profile/reddit/status", methods=["GET"])
@require_auth
def reddit_status():
    """Tests the saved cookie against Reddit so analysts know if it's still
    valid before a run. Reflects the real fetch path (same server, same cookie)."""
    cookie = db.get_reddit_cookie(g.user.id) if db.is_configured() else None
    if not cookie:
        log.info("reddit status check: no cookie saved (user=%s)", g.user.id)
        return jsonify({"state": "none"})
    ok = run_async(_check_reddit_cookie(cookie))
    log.info("reddit status check: %s (user=%s)", "active" if ok else "expired", g.user.id)
    return jsonify({"state": "active" if ok else "expired"})


async def _check_reddit_cookie(cookie: str) -> bool:
    try:
        async with httpx.AsyncClient(
            cookies={"reddit_session": cookie},
            headers={"User-Agent": config.BROWSER_UA},
        ) as c:
            r = await c.get("https://www.reddit.com/api/me.json", follow_redirects=True, timeout=15)
            log.debug("reddit cookie check response: status=%s", r.status_code)
            if r.status_code == 200:
                return bool((r.json() or {}).get("data", {}).get("name"))
    except Exception as e:
        log.warning("reddit cookie check failed: %s: %s", type(e).__name__, e)
    return False


# --- classification --------------------------------------------------------

@app.route("/api/extract", methods=["POST"])
@require_auth
def extract():
    if "file" not in request.files:
        log.warning("POST /api/extract rejected: no file uploaded (user=%s)", g.user.id)
        return jsonify({"error": "No file uploaded"}), 400
    f = request.files["file"]
    log.info("extracting URLs from upload: filename=%r (user=%s)", f.filename, g.user.id)
    try:
        df = pd.read_excel(f)
    except Exception as e:
        log.exception("could not parse uploaded Excel: filename=%r (user=%s)", f.filename, g.user.id)
        return jsonify({"error": f"Could not read Excel: {e}"}), 400

    url_col = _find_col(df, PERMALINK_HINTS)
    if not url_col:
        log.warning("no URL column found in %r — columns=%s", f.filename, list(df.columns))
        return jsonify({"error": f"No URL column found. Columns: {list(df.columns)}"}), 400

    urls = [str(u).strip() for u in df[url_col].dropna() if str(u).strip().lower() != "nan"]
    brand = infer_brand(df, _find_col(df, TOPIC_HINTS)) or ""
    log.info("extracted %d URLs from %r, inferred brand=%r (user=%s)", len(urls), f.filename, brand, g.user.id)
    return jsonify({"urls": urls, "brand": brand, "count": len(urls)})


@app.route("/api/classify", methods=["POST"])
@require_auth
def classify():
    data = request.get_json(force=True)
    urls = [u.strip() for u in data.get("urls", []) if u and u.strip()]
    brand = (data.get("brand") or "").strip()
    fetch_mode = data.get("fetch_mode", "cdp" if ALLOW_CDP else "reddit_cookie")
    if not ALLOW_CDP and fetch_mode == "cdp":
        fetch_mode = "reddit_cookie"
    if not urls:
        log.warning("POST /api/classify rejected: no URLs (user=%s)", g.user.id)
        return jsonify({"error": "No URLs provided"}), 400
    if not brand:
        log.warning("POST /api/classify rejected: no brand (user=%s)", g.user.id)
        return jsonify({"error": "Please choose a brand"}), 400

    log.info("classify start: brand=%r urls=%d fetch_mode=%r model=%s (user=%s)",
              brand, len(urls), fetch_mode, config.MODEL, g.user.id)
    try:
        results = run_async(_classify_urls(urls, brand, fetch_mode, g.user.id))
    except AuthenticationError:
        log.error("classify failed: invalid/missing ANTHROPIC_API_KEY (user=%s)", g.user.id)
        return jsonify({"error": "Invalid or missing ANTHROPIC_API_KEY (server config)."}), 400
    except APIStatusError as e:
        msg = str(getattr(e, "message", e))
        if "credit" in msg.lower() or "billing" in msg.lower():
            log.error("classify failed: Anthropic credit balance exhausted (user=%s)", g.user.id)
            return jsonify({"error": "Anthropic API has no credit balance — add credits and retry."}), 400
        log.exception("classify failed: Anthropic API error (user=%s)", g.user.id)
        return jsonify({"error": f"Anthropic API error: {msg}"}), 400
    except Exception as e:
        log.exception("classify failed: unexpected error (brand=%r, user=%s)", brand, g.user.id)
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 500

    applied = sum(1 for r in results if r.get("tag"))
    log.info("classify done: brand=%r total=%d tagged=%d (user=%s)",
              brand, len(results), applied, g.user.id)

    run_record = None
    if db.is_configured():
        try:
            run_record = db.save_run(g.user.id, brand, results, status="classified")
            log.info("run saved to history: id=%s (user=%s)", run_record.get("id"), g.user.id)
        except Exception:
            log.exception("failed to save run to history (non-fatal, user=%s)", g.user.id)

    return jsonify({"run_brand": brand, "results": results,
                     "run_id": run_record["id"] if run_record else None})


async def _classify_urls(urls, brand, fetch_mode, user_id):
    posts = [{"permalink": u, "excerpt": ""} for u in urls]

    log.info("fetch start: mode=%r posts=%d", fetch_mode, len(posts))
    if fetch_mode == "cdp":
        posts = await fetch_via_cdp(posts)
    elif fetch_mode == "reddit_cookie":
        cookie = db.get_reddit_cookie(user_id) if db.is_configured() else None
        if not cookie:
            log.warning("fetch_mode=reddit_cookie but no cookie saved for user=%s — fetch will be empty", user_id)
        posts = await fetch_via_reddit_cookie(posts, cookie)
    else:
        sem = asyncio.Semaphore(config.FETCH_CONCURRENCY)
        async with httpx.AsyncClient() as http:
            async def _f(p):
                async with sem:
                    await fetch_and_enrich(http, p)
                return p
            posts = await asyncio.gather(*[_f(p) for p in posts])

    got_text = sum(1 for p in posts if p.get("text"))
    log.info("fetch done: mode=%r got_text=%d/%d", fetch_mode, got_text, len(posts))
    if got_text < len(posts) * 0.5:
        log.warning("fetch got text for less than half the posts (mode=%r) — "
                     "classifications for the rest will be unreliable", fetch_mode)

    # Brand config (custom tag labels + per-tag rules). Empty when nothing is
    # configured -> classify_web falls back to the default behaviour exactly.
    brand_cfg = db.brand_config(brand) if db.is_configured() else {"labels": {}, "rules": {}, "roll_up_terms": []}
    n_rules = len(brand_cfg.get("rules") or {})
    log.info("brand config resolved: brand=%r custom_labels=%d rules=%d",
              brand, len(brand_cfg.get("labels") or {}), n_rules)

    anthropic = AsyncAnthropic()
    sem = asyncio.Semaphore(config.CLASSIFY_CONCURRENCY)
    decisions = await asyncio.gather(
        *[classify_web.classify_post(
            anthropic, config.MODEL, brand, p["permalink"], p.get("text", ""), sem, brand_cfg,
            content_type=p.get("content_type", "post"),
            post_text=p.get("post_text", ""),
            comment_text=p.get("comment_text", ""),
        ) for p in posts]
    )

    errors = [d for d in decisions if "classification error" in (d.get("reason") or "")]
    if errors:
        log.warning("%d/%d posts had a classification error, e.g.: %s",
                     len(errors), len(decisions), errors[0].get("reason"))

    out = []
    for d in decisions:
        tag = d.get("tag") or ""
        sentiment = tag.split(" - ", 1)[1] if tag and " - " in tag else ""
        out.append({
            "permalink": d["permalink"],
            "action": d.get("action"),
            "tag": tag,
            "sentiment": sentiment or ("—" if d.get("action") != "apply" else ""),
            "flag_brand": d.get("flag_brand", ""),
            "reason": d.get("reason", ""),
            "content_type": d.get("content_type", "post"),
        })
    return out


# --- export ------------------------------------------------------------------

@app.route("/api/export", methods=["POST"])
@require_auth
def export():
    data = request.get_json(force=True)
    results = data.get("results", [])
    brand = data.get("run_brand", "run")
    rows = [{
        "permalink": r.get("permalink"),
        "type": (r.get("content_type") or "post").capitalize(),
        "tag": r.get("tag", ""),
        "sentiment": r.get("sentiment", ""),
        "action": r.get("action", ""),
        "reason": r.get("reason", ""),
        "applied": "Yes" if r.get("applied") else "",
    } for r in results]
    df = pd.DataFrame(rows)
    buf = io.BytesIO()
    df.to_excel(buf, index=False)
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name=f"tagging_{brand}.xlsx",
                      mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


# --- apply to meltwater --------------------------------------------------------

@app.route("/api/apply", methods=["POST"])
@require_auth
def apply_to_meltwater():
    data = request.get_json(force=True)
    results = data.get("results", [])
    brand_name = (data.get("run_brand") or "").strip()
    run_id = data.get("run_id")
    applyable = sum(1 for r in results if r.get("action") == "apply" and r.get("tag"))
    log.info("apply start: brand=%r results=%d applyable=%d run_id=%s (user=%s)",
              brand_name, len(results), applyable, run_id, g.user.id)

    if not results:
        log.warning("apply rejected: no results provided (user=%s)", g.user.id)
        return jsonify({"error": "No results to apply."}), 400

    # Prefer email/password login automation (the same mechanism a real user
    # goes through, so it's the most likely to keep working as Meltwater's app
    # evolves). Session injection is kept as a fallback only for accounts with
    # no login saved -- live testing showed Meltwater's app does more
    # server-side session validation than a locally-cached token satisfies, so
    # it isn't reliable as a standalone method.
    creds = db.get_meltwater_creds_full(g.user.id) if db.is_configured() else None
    session_value = None
    if not creds:
        session_value = db.get_meltwater_session(g.user.id) if db.is_configured() else None
        if not session_value:
            log.warning("apply rejected: no Meltwater session or credentials saved (user=%s)", g.user.id)
            return jsonify({"error": "Add your Meltwater login on your Profile page first."}), 400

    brand = db.get_brand(brand_name) if brand_name else None
    topic_url = db.resolve_topic_url(g.user.id, brand) if brand else None
    if not topic_url:
        log.warning("apply rejected: no topic URL resolved for brand=%r (user=%s)", brand_name, g.user.id)
        return jsonify({"error": f"No Meltwater topic URL configured for '{brand_name}' for your "
                                  "account. Open Brand Studio -> select the brand -> 'My Meltwater "
                                  "topic URL' and paste the exact saved-search URL from YOUR "
                                  "Meltwater account (topic names often differ per account)."}), 400
    log.info("apply: resolved topic_url for brand=%r via=%s (user=%s)",
              brand_name, "session" if session_value else "login", g.user.id)

    try:
        if session_value:
            report = run_async(apply_via_session(session_value, topic_url, results))
        else:
            report = run_async(apply_results_to_meltwater(
                creds["meltwater_email"], creds["meltwater_password"], topic_url, results
            ))
    except Exception as e:
        log.exception("apply failed: unexpected error (brand=%r, user=%s)", brand_name, g.user.id)
        msg = str(e)
        if "Executable doesn't exist" in msg and "playwright install" in msg:
            # Deployment/build issue, not a data or login problem -- give the
            # analyst something actionable instead of a raw stack trace, and
            # log the real cause clearly for whoever manages the deploy.
            log.error("apply: Playwright's Chromium browser is not installed on this "
                       "server. The Render build step (render-build.sh) must run "
                       "'python -m playwright install --with-deps chromium' successfully. "
                       "Trigger a 'Clear build cache & deploy' on Render to fix this.")
            return jsonify({"error": (
                "The server that applies tags to Meltwater isn't fully set up yet "
                "(a required browser component is missing). This is a deployment "
                "issue, not something wrong with your data — please let whoever "
                "manages the deployment know, or try again in a few minutes if a "
                "deploy is in progress."
            )}), 500
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 500

    applied_links = {a["permalink"] for a in report.get("applied", [])}
    already_links = {a["permalink"] for a in report.get("skipped_already", [])}
    confirmed_links = applied_links | already_links  # both mean "tagged in Meltwater right now"

    if report.get("ok"):
        log.info("apply done: brand=%r applied=%d failed=%d already_tagged=%d (user=%s)",
                  brand_name, len(applied_links), len(report.get("failed", [])),
                  len(already_links), g.user.id)
    else:
        log.error("apply did not succeed: brand=%r message=%r (user=%s)",
                   brand_name, report.get("message"), g.user.id)

    if run_id and db.is_configured():
        try:
            # IMPORTANT: always merge into the run's FULL stored results, fetched
            # fresh from the DB -- never persist back just the `results` this
            # request happened to submit. A caller may legitimately send only a
            # subset (e.g. applying a single post from History), and writing
            # that subset back as-if it were the whole run would silently wipe
            # out every other row in the run.
            full_run = db.get_run(g.user.id, run_id)
            full_results = full_run["results"] if full_run and full_run.get("results") else results
            for r in full_results:
                if r.get("permalink") in confirmed_links:
                    r["applied"] = True
            any_confirmed = any(r.get("applied") for r in full_results)
            prior_status = full_run.get("status") if full_run else None
            new_status = "applied" if any_confirmed else (prior_status or "classified")
            db.update_run_after_apply(run_id, full_results, new_status)
        except Exception:
            log.exception("failed to persist apply results (non-fatal, run_id=%s)", run_id)

    status_code = 200 if report.get("ok") else 400
    return jsonify(report), status_code


# --- history -------------------------------------------------------------

@app.route("/api/history", methods=["GET"])
@require_auth
def history_list():
    return jsonify({"runs": db.list_runs(g.user.id)})


@app.route("/api/history/<run_id>", methods=["GET"])
@require_auth
def history_detail(run_id):
    run = db.get_run(g.user.id, run_id)
    if not run:
        return jsonify({"error": "Run not found"}), 404
    return jsonify({"run": run})


@app.route("/api/history/<run_id>", methods=["DELETE"])
@require_auth
def history_delete(run_id):
    if not db.is_configured():
        return jsonify({"error": "History storage is not configured."}), 400
    db.delete_run(g.user.id, run_id)
    return jsonify({"ok": True})


if __name__ == "__main__":
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "5000"))
    app.run(host=host, port=port, debug=False)
