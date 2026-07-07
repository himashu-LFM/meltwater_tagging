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
    fetch_full_text, fetch_via_cdp, classify_post, _find_col,
    PERMALINK_HINTS, infer_brand, TOPIC_HINTS,
)
import httpx

import db
from auth import require_auth
from fetchers import fetch_via_reddit_cookie
from meltwater_apply import apply_results_to_meltwater

app = Flask(__name__, static_folder="static", template_folder="templates")

# CDP fetch needs a local logged-in Chrome, which a cloud host doesn't have.
ALLOW_CDP = os.environ.get("MELTWATER_ALLOW_CDP", "true").lower() == "true"


def run_async(coro):
    return asyncio.run(coro)


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


# --- brands --------------------------------------------------------------

@app.route("/api/brands", methods=["GET"])
@require_auth
def get_brands():
    try:
        return jsonify({"brands": db.list_brands()})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/brands", methods=["POST"])
@require_auth
def upsert_brand_route():
    data = request.get_json(force=True)
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Brand name is required"}), 400
    brand = db.upsert_brand(
        name,
        roll_up_terms=data.get("roll_up_terms"),
        meltwater_topic_url=data.get("meltwater_topic_url"),
    )
    return jsonify({"brand": brand})


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
    db.delete_brand(brand_id)
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
        return jsonify({"error": "Meltwater email is required"}), 400
    db.upsert_meltwater_creds(g.user.id, email, password)
    return jsonify({"ok": True})


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
        return jsonify({"error": "Cookie value is required"}), 400
    db.upsert_reddit_cookie(g.user.id, cookie)
    return jsonify({"ok": True})


# --- classification --------------------------------------------------------

@app.route("/api/extract", methods=["POST"])
@require_auth
def extract():
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
    f = request.files["file"]
    try:
        df = pd.read_excel(f)
    except Exception as e:
        return jsonify({"error": f"Could not read Excel: {e}"}), 400

    url_col = _find_col(df, PERMALINK_HINTS)
    if not url_col:
        return jsonify({"error": f"No URL column found. Columns: {list(df.columns)}"}), 400

    urls = [str(u).strip() for u in df[url_col].dropna() if str(u).strip().lower() != "nan"]
    brand = infer_brand(df, _find_col(df, TOPIC_HINTS)) or ""
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
        return jsonify({"error": "No URLs provided"}), 400
    if not brand:
        return jsonify({"error": "Please choose a brand"}), 400

    try:
        results = run_async(_classify_urls(urls, brand, fetch_mode, g.user.id))
    except AuthenticationError:
        return jsonify({"error": "Invalid or missing ANTHROPIC_API_KEY (server config)."}), 400
    except APIStatusError as e:
        msg = str(getattr(e, "message", e))
        if "credit" in msg.lower() or "billing" in msg.lower():
            return jsonify({"error": "Anthropic API has no credit balance — add credits and retry."}), 400
        return jsonify({"error": f"Anthropic API error: {msg}"}), 400
    except Exception as e:
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 500

    run_record = None
    if db.is_configured():
        try:
            run_record = db.save_run(g.user.id, brand, results, status="classified")
        except Exception:
            pass  # history is best-effort; don't fail the classify response over it

    return jsonify({"run_brand": brand, "results": results,
                     "run_id": run_record["id"] if run_record else None})


async def _classify_urls(urls, brand, fetch_mode, user_id):
    posts = [{"permalink": u, "excerpt": ""} for u in urls]

    if fetch_mode == "cdp":
        posts = await fetch_via_cdp(posts)
    elif fetch_mode == "reddit_cookie":
        cookie = db.get_reddit_cookie(user_id) if db.is_configured() else None
        posts = await fetch_via_reddit_cookie(posts, cookie)
    else:
        sem = asyncio.Semaphore(config.FETCH_CONCURRENCY)
        async with httpx.AsyncClient() as http:
            async def _f(p):
                async with sem:
                    p["text"] = await fetch_full_text(http, p["permalink"], p["excerpt"])
                return p
            posts = await asyncio.gather(*[_f(p) for p in posts])

    anthropic = AsyncAnthropic()
    sem = asyncio.Semaphore(config.CLASSIFY_CONCURRENCY)
    decisions = await asyncio.gather(
        *[classify_post(anthropic, brand, p["permalink"], p.get("text", ""), sem) for p in posts]
    )

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
        "tag": r.get("tag", ""),
        "sentiment": r.get("sentiment", ""),
        "action": r.get("action", ""),
        "reason": r.get("reason", ""),
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
    if not results:
        return jsonify({"error": "No results to apply."}), 400

    creds = db.get_meltwater_creds_full(g.user.id)
    if not creds:
        return jsonify({"error": "Add your Meltwater login on your Profile page first."}), 400

    brand = db.get_brand(brand_name) if brand_name else None
    topic_url = brand.get("meltwater_topic_url") if brand else None
    if not topic_url:
        return jsonify({"error": f"'{brand_name}' has no Meltwater topic URL configured yet. "
                                  "Set it once on the Profile page under Brand settings."}), 400

    try:
        report = run_async(apply_results_to_meltwater(
            creds["meltwater_email"], creds["meltwater_password"], topic_url, results
        ))
    except Exception as e:
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 500

    if run_id and db.is_configured() and report.get("ok"):
        try:
            db.update_run_status(run_id, "applied")
        except Exception:
            pass

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


if __name__ == "__main__":
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "5000"))
    app.run(host=host, port=port, debug=False)
