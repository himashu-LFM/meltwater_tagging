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
import re
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

# Windows consoles default to cp1252; a stray non-ASCII print from any library
# (e.g. a "→"/"⚠" in a log line) would otherwise raise UnicodeEncodeError and
# fail the whole request. Make stdout/stderr tolerant so prints never crash us.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import config
from classify import (
    fetch_full_text, fetch_and_enrich, fetch_via_cdp, fetch_reddit_scraper_bulk, fetch_via_apify,
    classify_post, _find_col,
    PERMALINK_HINTS, infer_brand, TOPIC_HINTS,
)
import httpx

import db
import emailer
from auth import require_auth
from fetchers import fetch_via_reddit_cookie
from meltwater_apply import (
    apply_results_to_meltwater, apply_via_session, apply_via_api, decode_session_expiry,
    is_meltwater_sso_email,
)
import classify_web
from logging_setup import get_logger

log = get_logger("app")

app = Flask(__name__, static_folder="static", template_folder="templates")

# CDP fetch needs a local logged-in Chrome, which a cloud host doesn't have.
ALLOW_CDP = os.environ.get("MELTWATER_ALLOW_CDP", "true").lower() == "true"

# Use the memory-safe API apply path (no feed rendering) with browser fallback.
# Set MELTWATER_USE_API=false to force the old browser tagger.
USE_API_APPLY = os.environ.get("MELTWATER_USE_API", "true").lower() == "true"


# --- MFA (Microsoft SSO OTP) human-in-the-loop bridge -----------------------
# For @meltwater.com logins the apply job pauses mid-login waiting for the SMS
# code. The Playwright browser runs inside ONE Flask request's event loop; the
# OTP arrives on a SEPARATE request (POST /api/mfa/otp). We bridge the two with
# a per-user, thread-safe store the async login callback polls. This requires
# the server to handle concurrent requests while one is parked — Flask's dev
# server runs threaded, and gunicorn is configured with --threads (see Procfile
# / Dockerfile).
import threading
import time as _time

_mfa_lock = threading.Lock()
_mfa_waiters: dict = {}  # user_id -> {round, state, attempt, max, error, otp}
MFA_WAIT_SECONDS = int(os.environ.get("MELTWATER_MFA_WAIT_SECONDS", "300"))  # 5 min per attempt


def _make_request_otp(user_id):
    """Return the async callback the SSO login flow calls when it needs the SMS
    code. It publishes an 'awaiting' state the frontend polls, then blocks
    cooperatively until the user submits a code (via /api/mfa/otp), cancels, or
    the wait elapses. Returns the code string, or None to abort the login."""
    async def request_otp(attempt, max_attempts, error):
        with _mfa_lock:
            w = _mfa_waiters.setdefault(user_id, {"round": 0})
            w.update({"state": "awaiting", "round": w.get("round", 0) + 1,
                      "attempt": attempt, "max": max_attempts, "error": error, "otp": None})
        log.info("mfa: awaiting OTP for user=%s attempt=%d/%d error=%r",
                  user_id, attempt, max_attempts, error)
        deadline = _time.monotonic() + MFA_WAIT_SECONDS
        while _time.monotonic() < deadline:
            with _mfa_lock:
                w = _mfa_waiters.get(user_id) or {}
                if w.get("state") == "submitted" and w.get("otp"):
                    code = w["otp"]
                    w["state"] = "processing"
                    w["otp"] = None
                    log.info("mfa: OTP received for user=%s attempt=%d", user_id, attempt)
                    return code
                if w.get("state") == "cancelled":
                    log.info("mfa: OTP cancelled by user=%s", user_id)
                    return None
            await asyncio.sleep(1)
        with _mfa_lock:
            w = _mfa_waiters.get(user_id) or {}
            w["state"] = "timeout"
        log.warning("mfa: OTP wait timed out for user=%s attempt=%d", user_id, attempt)
        return None
    return request_otp


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
            environment=data.get("environment"),
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
        environment=data.get("environment"),
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


# --- brand feedback docs (taxonomy brands like Bentley) ----------------------

def _extract_doc_text(filename: str, data: bytes) -> tuple[str | None, str | None]:
    """Return (text, error). Supports .txt/.md/.csv and .docx (stdlib, no dep)."""
    name = (filename or "").lower()
    try:
        if name.endswith((".txt", ".md", ".csv")):
            return data.decode("utf-8", "ignore").strip(), None
        if name.endswith(".docx"):
            import io, zipfile, re, html
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                xml = z.read("word/document.xml").decode("utf-8", "ignore")
            xml = re.sub(r"</w:p>", "\n", xml)          # paragraph breaks
            text = re.sub(r"<[^>]+>", "", xml)           # strip all tags
            return html.unescape(text).strip(), None
        return None, "Unsupported file — upload .docx, .txt, or .md (PDF support coming later)."
    except Exception as e:
        return None, f"Could not read the file: {e}"


@app.route("/api/brands/<int:brand_id>/feedback-docs", methods=["GET"])
@require_auth
def list_feedback_docs_route(brand_id):
    brand = db.get_brand_by_id(brand_id)
    if not brand:
        return jsonify({"error": "brand not found"}), 404
    return jsonify({"docs": db.list_feedback_docs(brand["name"])})


@app.route("/api/brands/<int:brand_id>/feedback-docs", methods=["POST"])
@require_auth
def upload_feedback_doc_route(brand_id):
    brand = db.get_brand_by_id(brand_id)
    if not brand:
        return jsonify({"error": "brand not found"}), 404
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"error": "No file uploaded."}), 400
    text, err = _extract_doc_text(f.filename, f.read())
    if err:
        return jsonify({"error": err}), 400
    if not text:
        return jsonify({"error": "The file appears to be empty."}), 400
    row = db.save_feedback_doc(brand["id"], brand["name"], f.filename, text,
                               uploaded_by=g.user.id)
    doc_id = row.get("id")

    # Parse the doc into reusable rules and store them. Best-effort: if extraction
    # fails, the doc is still saved and we report the error without 500-ing.
    rules_added, extract_error = 0, None
    try:
        from brands.bentley.extract_rules import extract_rules
        rules = extract_rules(text)
        rules_added = db.save_feedback_rules(brand["id"], brand["name"], doc_id, rules,
                                             created_by=g.user.id)
    except Exception as e:
        extract_error = str(e)
        log.exception("rule extraction failed for doc %s", doc_id)

    return jsonify({"ok": True,
                    "doc": {"id": doc_id, "filename": f.filename, "chars": len(text)},
                    "rules_added": rules_added,
                    "extract_error": extract_error})


@app.route("/api/brands/<int:brand_id>/feedback-rules", methods=["GET"])
@require_auth
def list_feedback_rules_route(brand_id):
    brand = db.get_brand_by_id(brand_id)
    if not brand:
        return jsonify({"error": "brand not found"}), 404
    return jsonify({"rules": db.list_feedback_rules(brand["name"], active_only=False)})


@app.route("/api/brands/<int:brand_id>/feedback-rules/<rule_id>", methods=["DELETE"])
@require_auth
def delete_feedback_rule_route(brand_id, rule_id):
    db.delete_feedback_rule(rule_id)
    return jsonify({"ok": True})


@app.route("/api/brands/<int:brand_id>/feedback-docs/<doc_id>", methods=["DELETE"])
@require_auth
def delete_feedback_doc_route(brand_id, doc_id):
    db.delete_feedback_doc(doc_id)
    return jsonify({"ok": True})


# --- profile: meltwater + reddit creds ---------------------------------------

@app.route("/api/auth/welcome", methods=["POST"])
def auth_welcome():
    """Send a welcome email right after a new account is created. Called by the
    signup page after Supabase signUp succeeds. Best-effort: a failure here never
    blocks the signup (the account already exists in Supabase)."""
    from datetime import datetime, timezone
    data = request.get_json(force=True, silent=True) or {}
    email = (data.get("email") or "").strip()
    if not email or "@" not in email:
        return jsonify({"ok": False, "error": "valid email required"}), 400
    when = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    sent = emailer.send_welcome_email(email, when)
    return jsonify({"ok": True, "emailed": sent})


@app.route("/api/auth/forgot-password", methods=["POST"])
def auth_forgot_password():
    """Start a password reset: if the email is registered, email a 6-digit code;
    otherwise tell the caller it's not registered."""
    import hashlib
    import secrets
    from datetime import datetime, timezone, timedelta
    data = request.get_json(force=True, silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    if not email or "@" not in email:
        return jsonify({"ok": False, "error": "Enter a valid email address."}), 400
    if not db.is_configured():
        return jsonify({"ok": False, "error": "Server is not configured."}), 500

    user = db.find_auth_user_by_email(email)
    if not user:
        log.info("forgot-password: no account for %s", email)
        return jsonify({"ok": False, "registered": False,
                        "error": "No account is registered with this email."}), 404

    code = f"{secrets.randbelow(1_000_000):06d}"
    code_hash = hashlib.sha256(code.encode()).hexdigest()
    expires = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
    try:
        db.upsert_reset_code(email, code_hash, expires)
    except Exception:
        log.exception("forgot-password: could not store reset code for %s", email)
        return jsonify({"ok": False, "error": "Could not start the reset. Try again."}), 500

    sent = emailer.send_reset_code_email(email, code, minutes=10)
    if not sent:
        return jsonify({"ok": False, "error": "Couldn't send the code email — check email setup."}), 500
    log.info("forgot-password: reset code emailed to %s", email)
    return jsonify({"ok": True})


@app.route("/api/auth/reset-password", methods=["POST"])
def auth_reset_password():
    """Verify the emailed code and set the new password."""
    import hashlib
    from datetime import datetime, timezone
    data = request.get_json(force=True, silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    code = (data.get("code") or "").strip()
    new_password = data.get("new_password") or ""
    if not email or not code:
        return jsonify({"ok": False, "error": "Email and code are required."}), 400
    if len(new_password) < 6:
        return jsonify({"ok": False, "error": "Password must be at least 6 characters."}), 400
    if not db.is_configured():
        return jsonify({"ok": False, "error": "Server is not configured."}), 500

    rec = db.get_reset_code(email)
    if not rec:
        return jsonify({"ok": False, "error": "No reset in progress — request a new code."}), 400

    # expiry
    try:
        exp = datetime.fromisoformat(str(rec["expires_at"]).replace("Z", "+00:00"))
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
    except Exception:
        exp = None
    if exp is None or datetime.now(timezone.utc) > exp:
        db.delete_reset_code(email)
        return jsonify({"ok": False, "error": "That code has expired — request a new one."}), 400

    # attempt limit
    attempts = int(rec.get("attempts") or 0)
    if attempts >= 5:
        db.delete_reset_code(email)
        return jsonify({"ok": False, "error": "Too many attempts — request a new code."}), 429

    if hashlib.sha256(code.encode()).hexdigest() != rec["code_hash"]:
        db.bump_reset_attempts(email, attempts + 1)
        return jsonify({"ok": False, "error": "Incorrect code. Check it and try again."}), 400

    user = db.find_auth_user_by_email(email)
    if not user:
        db.delete_reset_code(email)
        return jsonify({"ok": False, "error": "No account is registered with this email."}), 404

    if not db.update_auth_user_password(user["id"], new_password):
        log.error("reset-password: password update failed for %s", email)
        return jsonify({"ok": False, "error": "Could not update the password. Try again."}), 500

    db.delete_reset_code(email)
    log.info("reset-password: password updated for %s", email)
    return jsonify({"ok": True})


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
        # Did the Meltwater login actually change? (email changed, or a new
        # password was entered). If so, the saved browser session belongs to the
        # OLD login and must be cleared so the next Apply logs in fresh with the
        # new credentials. Re-saving the same email with a blank password leaves
        # the session intact (no needless OTP).
        old = db.get_meltwater_creds(g.user.id) if db.is_configured() else None
        old_email = ((old or {}).get("meltwater_email") or "").strip().lower()
        creds_changed = (old_email != email.strip().lower()) or bool(password)

        db.upsert_meltwater_creds(g.user.id, email, password)
        # never log the password value itself
        log.info("Meltwater creds saved for user=%s (password_changed=%s, creds_changed=%s)",
                  g.user.id, bool(password), creds_changed)

        cleared = False
        if creds_changed and db.is_configured():
            try:
                db.clear_meltwater_browser_state(g.user.id)
                cleared = True
                log.info("Meltwater creds changed for user=%s — cleared saved browser session "
                          "so the new login is used on the next Apply", g.user.id)
            except Exception:
                log.exception("Could not clear browser session after creds change (user=%s)", g.user.id)

        return jsonify({"ok": True, "session_cleared": cleared})
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


# --- Auto-captured Meltwater BROWSER session (SSO no-repeat-OTP) --------------

@app.route("/api/profile/meltwater-browser-session/status", methods=["GET"])
@require_auth
def meltwater_browser_session_status():
    """Whether an auto-captured SSO session is saved (so the UI can show it and
    offer 'Log out of Meltwater')."""
    meta = db.get_meltwater_browser_state_meta(g.user.id) if db.is_configured() else None
    if not meta:
        return jsonify({"state": "none"})
    return jsonify({"state": "saved", "updated_at": meta.get("updated_at"),
                    "expires_at": meta.get("expires_at")})


@app.route("/api/profile/meltwater-browser-session", methods=["DELETE"])
@require_auth
def clear_meltwater_browser_session():
    """Log out of Meltwater: delete the saved SSO browser session. The next Apply
    will do a fresh login (one SMS OTP) and save a new session."""
    try:
        if db.is_configured():
            db.clear_meltwater_browser_state(g.user.id)
        log.info("Meltwater browser session cleared for user=%s", g.user.id)
        return jsonify({"ok": True})
    except Exception as e:
        log.exception("DELETE /api/profile/meltwater-browser-session failed (user=%s)", g.user.id)
        return jsonify({"error": str(e)}), 500


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
    # Default to the Reddit scraper (public RSS): the cookie path is 403'd by
    # Reddit and CDP hits a "prove your humanity" CAPTCHA, so neither is
    # reliable, and the RSS route needs no credentials at all.
    # Prefer Apify when it's configured (one record per URL, no Reddit rate
    # limit); otherwise fall back to the credential-free RSS scraper.
    fetch_mode = data.get("fetch_mode") or ("apify" if config.APIFY_TOKEN else "reddit_scraper")
    if not ALLOW_CDP and fetch_mode == "cdp":
        fetch_mode = "reddit_scraper"
    if not urls:
        log.warning("POST /api/classify rejected: no URLs (user=%s)", g.user.id)
        return jsonify({"error": "No URLs provided"}), 400
    if not brand:
        log.warning("POST /api/classify rejected: no brand (user=%s)", g.user.id)
        return jsonify({"error": "Please choose a brand"}), 400

    # Route by brand style: taxonomy brands (Bentley) use the multi-tag pipeline
    # with scope + feedback rules; sentiment brands (Kaseya/Ninja) use the old path.
    from brands import get_profile
    try:
        is_taxonomy = get_profile(brand).style == "taxonomy"
    except Exception:
        is_taxonomy = False

    log.info("classify start: brand=%r style=%s urls=%d fetch_mode=%r model=%s (user=%s)",
              brand, "taxonomy" if is_taxonomy else "sentiment", len(urls), fetch_mode, config.MODEL, g.user.id)
    try:
        if is_taxonomy:
            results = _classify_bentley_urls(urls)
        else:
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

    # Tag label per sentiment, so the results table can offer a manual override
    # that produces exactly the tag string Meltwater expects. Sentiment brands
    # only — taxonomy brands (Bentley) use multi-tag labels, so we send none and
    # the UI leaves those rows read-only.
    labels = _override_labels(brand, is_taxonomy)

    return jsonify({"run_brand": brand, "results": results, "labels": labels,
                     "run_id": run_record["id"] if run_record else None})


def _override_labels(brand: str, is_taxonomy: bool = False) -> dict:
    """Tag label per sentiment for the manual-override dropdown."""
    if is_taxonomy:
        return {}
    try:
        cfg = db.brand_config(brand) if db.is_configured() else {"labels": {}}
        return {s: classify_web._label_for(brand, s, cfg.get("labels", {}))
                for s in ("positive", "negative", "neutral")}
    except Exception:
        log.exception("could not resolve override labels for %r (non-fatal)", brand)
        return {s: f"{s.capitalize()} - {brand}" for s in ("positive", "negative", "neutral")}


def _is_retryable(r: dict) -> bool:
    """A row worth re-classifying: the model never reached a verdict.

    Deliberately excludes rows that are already decided — `apply` (including
    deleted-on-Reddit Neutrals), `skip_flag` (a genuinely different brand), and
    anything a human set by hand, which must never be overwritten by a retry."""
    return r.get("action") == "review" and not r.get("overridden")


@app.route("/api/reclassify", methods=["POST"])
@require_auth
def reclassify():
    """Re-run classification for the failed rows ONLY, merging them back into the
    existing run. Rows that already succeeded are never re-sent, so a retry costs
    a fraction of a full re-run (both Claude tokens and Apify records)."""
    data = request.get_json(force=True)
    results = data.get("results", [])
    brand = (data.get("run_brand") or "").strip()
    run_id = data.get("run_id")
    fetch_mode = data.get("fetch_mode") or ("apify" if config.APIFY_TOKEN else "reddit_scraper")
    if not ALLOW_CDP and fetch_mode == "cdp":
        fetch_mode = "reddit_scraper"

    if not results or not brand:
        return jsonify({"error": "Nothing to retry (missing results or brand)."}), 400

    retry_idx = [i for i, r in enumerate(results) if _is_retryable(r)]
    retry_urls = [results[i].get("permalink") for i in retry_idx if results[i].get("permalink")]
    if not retry_urls:
        return jsonify({"error": "No failed rows to retry."}), 400

    from brands import get_profile
    try:
        is_taxonomy = get_profile(brand).style == "taxonomy"
    except Exception:
        is_taxonomy = False

    log.info("reclassify start: brand=%r retrying=%d/%d fetch_mode=%r (user=%s)",
             brand, len(retry_urls), len(results), fetch_mode, g.user.id)
    try:
        if is_taxonomy:
            fresh = _classify_bentley_urls(retry_urls)
        else:
            fresh = run_async(_classify_urls(retry_urls, brand, fetch_mode, g.user.id))
    except AuthenticationError:
        return jsonify({"error": "Invalid or missing ANTHROPIC_API_KEY (server config)."}), 400
    except APIStatusError as e:
        msg = getattr(e, "message", str(e))
        if "credit balance" in str(msg).lower():
            return jsonify({"error": "Anthropic API has no credit balance — add credits and retry."}), 400
        return jsonify({"error": f"Anthropic API error: {msg}"}), 400
    except Exception as e:
        log.exception("reclassify failed (brand=%r, user=%s)", brand, g.user.id)
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 500

    # Merge by permalink, and only where the retry actually did better — a row
    # that fails again keeps its original reason rather than churning.
    by_link = {r.get("permalink"): r for r in fresh}
    recovered = 0
    merged = list(results)
    for i in retry_idx:
        new = by_link.get(merged[i].get("permalink"))
        if not new:
            continue
        if new.get("action") != "review":
            recovered += 1
        merged[i] = new

    log.info("reclassify done: brand=%r retried=%d recovered=%d (user=%s)",
             brand, len(retry_urls), recovered, g.user.id)

    if run_id and db.is_configured():
        try:
            db.update_run_after_apply(run_id, merged, "classified")
        except Exception:
            log.exception("failed to update run %s after retry (non-fatal)", run_id)

    return jsonify({"run_brand": brand, "results": merged,
                    "labels": _override_labels(brand, is_taxonomy),
                    "run_id": run_id,
                    "retried": len(retry_urls), "recovered": recovered})


def _classify_bentley_urls(urls):
    """Bentley (taxonomy) classify path for the dashboard.

    Reuses the CLI batch runner (its own news fetcher + feedback-rule injection),
    then maps each result to the shape the results table/history expect. The
    'Apply to Meltwater' button stays disabled for now (action='review') because
    Bentley Phase-2 apply (multi-tag) isn't wired yet.
    """
    from brands.bentley.classify_batch import run as bentley_run
    raw = bentley_run(urls, workers=6)
    out = []
    for r in raw:
        tags = r.get("tags") or []
        in_scope = r.get("scope") == "in"
        out.append({
            "permalink": r.get("url"),
            "content_type": "post",
            "sentiment": "",                 # Bentley has no sentiment
            "scope": r.get("scope"),
            "tags": tags,                    # list — for the future brand-aware table
            "tags_by_family": r.get("tags_by_family") or {},
            "tag": ", ".join(tags) if tags else ("Not in scope" if r.get("scope") == "out" else "—"),
            "action": "review",             # keep Apply disabled until Phase-2 apply exists
            "reason": r.get("reason", ""),
            "needs_review": r.get("needs_review") or [],
        })
    return out


async def _classify_urls(urls, brand, fetch_mode, user_id):
    posts = [{"permalink": u, "excerpt": ""} for u in urls]

    log.info("fetch start: mode=%r posts=%d", fetch_mode, len(posts))
    if fetch_mode == "apify":
        # Paid Apify actor: one record per URL (post OR the specific comment),
        # no Reddit rate limit. Measured 36 mentions in 10s vs ~23 min on RSS.
        if not config.APIFY_TOKEN:
            log.warning("fetch_mode=apify but APIFY_TOKEN is not set — falling back "
                        "to the credential-free RSS scraper")
            posts = await fetch_reddit_scraper_bulk(posts)
        else:
            posts = await fetch_via_apify(posts)
        leftovers = [p for p in posts if not p.get("text") and not p.get("deleted")
                     and "reddit.com" not in (p.get("permalink") or "")]
        if leftovers:
            async with httpx.AsyncClient() as http:
                await asyncio.gather(*[fetch_and_enrich(http, p) for p in leftovers])
    elif fetch_mode == "reddit_scraper":
        # Credential-free public RSS, grouped by THREAD — one call per thread
        # instead of one per mention (real data: 53 mentions across 21 threads
        # = 60% fewer calls), which is what makes the ~1-request-per-minute
        # anonymous budget workable at all.
        threads = len({(p.get("permalink") or "").split("/comments/")[-1].split("/")[0]
                       for p in posts if "/comments/" in (p.get("permalink") or "")})
        log.info("fetch[reddit_scraper/rss]: %d mention(s) across %d thread(s) — %d fewer request(s)",
                 len(posts), threads, max(0, len(posts) - threads))
        posts = await fetch_reddit_scraper_bulk(posts)
        # anything non-Reddit still needs the generic path
        leftovers = [p for p in posts if not p.get("text")
                     and "reddit.com" not in (p.get("permalink") or "")]
        if leftovers:
            async with httpx.AsyncClient() as http:
                await asyncio.gather(*[fetch_and_enrich(http, p) for p in leftovers])
    elif fetch_mode == "cdp":
        posts = await fetch_via_cdp(posts)
    elif fetch_mode == "reddit_cookie":
        cookie = db.get_reddit_cookie(user_id) if db.is_configured() else None
        if not cookie:
            log.warning("fetch_mode=reddit_cookie but no cookie saved for user=%s — fetch will be empty", user_id)
        posts = await fetch_via_reddit_cookie(posts, cookie)
    elif fetch_mode == "news_reader":
        # Interim: same generic fetch as "anon" (plain GET + tag-strip) until the
        # dedicated news-site reader (real article extraction, retries, paywall
        # handling, publication-country/byline metadata) is built.
        sem = asyncio.Semaphore(config.FETCH_CONCURRENCY)
        async with httpx.AsyncClient() as http:
            async def _f(p):
                async with sem:
                    await fetch_and_enrich(http, p)
                return p
            posts = await asyncio.gather(*[_f(p) for p in posts])
    else:
        # "reddit_scraper" (recommended) reads Reddit's PUBLIC Atom feed — no API
        # key, no login, no cookie, no CAPTCHA. "reddit_api" prefers the official
        # OAuth Data API (needs REDDIT_CLIENT_ID/SECRET). Either way the other
        # route is tried as a fallback, so one blocked path doesn't lose posts.
        # "anon" keeps the legacy behaviour (RSS first, then plain .json).
        prefer = "api" if fetch_mode == "reddit_api" else "rss"
        if fetch_mode == "reddit_api" and not (config.REDDIT_CLIENT_ID and config.REDDIT_CLIENT_SECRET):
            log.warning("fetch_mode=reddit_api but REDDIT_CLIENT_ID/REDDIT_CLIENT_SECRET are not "
                        "configured — falling back to the public RSS scraper path")
        sem = asyncio.Semaphore(config.FETCH_CONCURRENCY)
        async with httpx.AsyncClient() as http:
            async def _f(p):
                async with sem:
                    await fetch_and_enrich(http, p, prefer=prefer)
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
            deleted=bool(p.get("deleted")),
        ) for p in posts]
    )

    errors = [d for d in decisions if "classification error" in (d.get("reason") or "")]
    if errors:
        log.warning("%d/%d posts had a classification error, e.g.: %s",
                     len(errors), len(decisions), errors[0].get("reason"))

    out = []
    for d in decisions:
        tag = d.get("tag") or ""
        # Find the sentiment word anywhere in the tag — never assume which side of
        # the "-" it's on or how the tag is separated. We just locate whichever of
        # the three sentiment words appears (as a whole word), so a hyphenated
        # brand like "N-Able" is never mistaken for the sentiment.
        _m = re.search(r"\b(positive|negative|neutral)\b", tag, re.I)
        sentiment = _m.group(1).capitalize() if _m else ""
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
        # Audit trail for rows a human changed in the results table.
        "edited_by_user": "Yes" if r.get("overridden") else "",
        "model_sentiment": r.get("auto_sentiment", "") if r.get("overridden") else "",
        "applied": "Yes" if r.get("applied") else "",
    } for r in results]
    df = pd.DataFrame(rows)
    buf = io.BytesIO()
    df.to_excel(buf, index=False)
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name=f"tagging_{brand}.xlsx",
                      mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


# --- MFA (Microsoft SSO OTP) ---------------------------------------------------

@app.route("/api/mfa/status", methods=["GET"])
@require_auth
def mfa_status():
    """Frontend polls this during an apply run. When state == 'awaiting', the
    tagging screen shows the OTP popup. The code itself is never echoed back."""
    with _mfa_lock:
        w = dict(_mfa_waiters.get(g.user.id) or {})
    return jsonify({
        "state": w.get("state", "none"),
        "round": w.get("round", 0),
        "attempt": w.get("attempt"),
        "max": w.get("max"),
        "error": w.get("error"),
    })


@app.route("/api/mfa/otp", methods=["POST"])
@require_auth
def mfa_submit_otp():
    data = request.get_json(force=True)
    code = (data.get("otp") or "").strip()
    if not code:
        return jsonify({"error": "Enter the code first."}), 400
    with _mfa_lock:
        w = _mfa_waiters.get(g.user.id)
        if not w or w.get("state") != "awaiting":
            return jsonify({"error": "No verification is currently waiting for a code."}), 409
        w["otp"] = code
        w["state"] = "submitted"
    log.info("mfa: OTP submitted by user=%s", g.user.id)
    return jsonify({"ok": True})


@app.route("/api/mfa/cancel", methods=["POST"])
@require_auth
def mfa_cancel():
    with _mfa_lock:
        w = _mfa_waiters.get(g.user.id)
        if w:
            w["state"] = "cancelled"
    log.info("mfa: OTP cancelled via API by user=%s", g.user.id)
    return jsonify({"ok": True})


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

    # @meltwater.com accounts use the SSO UI pipeline (switch into the brand's
    # ENVIRONMENT account, then open its Reddit saved search by brand name) — they
    # need the brand's Environment set, not a topic URL. Everyone else (Auth0)
    # still needs a resolved topic URL as before.
    sso_account = bool(creds) and is_meltwater_sso_email(creds.get("meltwater_email", ""))
    environment = (brand or {}).get("environment") if brand else None

    if sso_account:
        if not environment:
            log.warning("apply rejected: SSO account but brand=%r has no Environment set (user=%s)",
                         brand_name, g.user.id)
            return jsonify({"error": f"'{brand_name}' has no Environment set. Open Brand Studio -> "
                                      "select the brand -> set 'Environment' to the exact Meltwater "
                                      "account name (e.g. 'Kaseya - Fairhair'), then try again."}), 400
        log.info("apply: SSO account — environment=%r brand=%r (user=%s)",
                  environment, brand_name, g.user.id)
    else:
        if not topic_url:
            log.warning("apply rejected: no topic URL resolved for brand=%r (user=%s)", brand_name, g.user.id)
            return jsonify({"error": f"No Meltwater topic URL configured for '{brand_name}' for your "
                                      "account. Open Brand Studio -> select the brand -> 'My Meltwater "
                                      "topic URL' and paste the exact saved-search URL from YOUR "
                                      "Meltwater account (topic names often differ per account)."}), 400
        log.info("apply: resolved topic_url for brand=%r via=%s (user=%s)",
                  brand_name, "session" if session_value else "login", g.user.id)

    # For @meltwater.com accounts the login pauses for an SMS code; this callback
    # lets the browser flow ask for it via the tagging-screen popup. Harmless for
    # ListenFirstMedia accounts (the SSO branch is the only caller). Clear any
    # leftover state from a prior run so its round counter can't re-trigger the
    # popup before this run actually needs a code.
    with _mfa_lock:
        _mfa_waiters.pop(g.user.id, None)
    request_otp = _make_request_otp(g.user.id)

    try:
        if session_value:
            report = run_async(apply_via_session(session_value, topic_url, results))
        else:
            # Prefer the memory-safe API path (no feed rendering). If it can't
            # capture the session/query, it flags a fallback and we use the
            # browser tagger instead so behaviour never regresses.
            report = None
            # @meltwater.com (SSO) analysts often have MULTIPLE Meltwater
            # workspaces; the SSO login lands on a personal one where the brand's
            # saved search 404s (empty feed), so the internal API path can't
            # capture anything and just wastes time. These accounts go straight
            # to the browser path, which switches into the brand's ENVIRONMENT
            # account and opens its Reddit saved search via Explore.
            # (sso_account was determined above alongside the environment lookup.)
            if sso_account:
                log.info("apply: %s is a Microsoft SSO account — browser path with account switch "
                          "(env=%r, brand=%r)", creds["meltwater_email"], environment, brand_name)
            if USE_API_APPLY and not sso_account:
                try:
                    report = run_async(apply_via_api(
                        creds["meltwater_email"], creds["meltwater_password"], topic_url, results,
                        request_otp,
                    ))
                    if report.get("_fallback"):
                        log.warning("apply: API path unavailable (%s) — falling back to browser tagger",
                                     report.get("message"))
                        report = None
                except Exception:
                    log.exception("apply: API path errored — falling back to browser tagger")
                    report = None
            if report is None:
                # SSO session reuse: load any saved browser session so we skip
                # login/OTP, and capture a fresh one after a first-time login.
                # While a saved session exists we never auto-prompt OTP (it's
                # cleared manually from Profile).
                saved_state = (db.get_meltwater_browser_state(g.user.id)
                               if (sso_account and db.is_configured()) else None)
                _uid = g.user.id

                def _on_state_captured(state_json):
                    try:
                        db.upsert_meltwater_browser_state(_uid, state_json)
                    except Exception:
                        log.exception("apply: could not save captured Meltwater session (user=%s)", _uid)

                report = run_async(apply_results_to_meltwater(
                    creds["meltwater_email"], creds["meltwater_password"], topic_url, results,
                    request_otp,
                    account_hint=(environment if sso_account else None),
                    brand_name=(brand_name if sso_account else None),
                    saved_state=saved_state,
                    on_state_captured=(_on_state_captured if sso_account else None),
                ))
                if report and report.get("_session_expired"):
                    log.warning("apply: saved SSO session expired for user=%s — asking user to clear it",
                                 g.user.id)
                    return jsonify({"error": report.get("message"), "session_expired": True}), 409
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
    # Same override labels the tagger screen gets, so a stored run can be
    # corrected by hand from History too.
    brand = run.get("brand_name") or ""
    try:
        from brands import get_profile
        is_taxonomy = get_profile(brand).style == "taxonomy"
    except Exception:
        is_taxonomy = False
    return jsonify({"run": run, "labels": _override_labels(brand, is_taxonomy)})


@app.route("/api/history/<run_id>/results", methods=["PUT"])
@require_auth
def history_update_results(run_id):
    """Persist edited rows for a stored run (manual sentiment overrides).

    History is the durable record, so an override made there has to be written
    back — unlike the tagger screen, where results live only in the page."""
    if not db.is_configured():
        return jsonify({"error": "History storage is not configured."}), 400
    run = db.get_run(g.user.id, run_id)          # also enforces ownership
    if not run:
        return jsonify({"error": "Run not found"}), 404

    data = request.get_json(force=True)
    results = data.get("results")
    if not isinstance(results, list) or not results:
        return jsonify({"error": "No results provided."}), 400
    if len(results) != len(run.get("results") or []):
        return jsonify({"error": "Result count does not match the stored run."}), 400

    edited = sum(1 for r in results if r.get("overridden"))
    db.update_run_after_apply(run_id, results, run.get("status") or "classified")
    log.info("history run %s updated: %d row(s) manually overridden (user=%s)",
             run_id, edited, g.user.id)
    return jsonify({"ok": True, "edited": edited})


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
    # threaded=True so a paused apply request (waiting on the MFA OTP) doesn't
    # block the concurrent /api/mfa/* requests that deliver the code.
    app.run(host=host, port=port, debug=False, threaded=True)
