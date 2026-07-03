"""
Web UI for the Meltwater sentiment tagger (classification + export).

Run:
    python webapp/app.py
Then open http://127.0.0.1:5000

Reuses the same fetch + classify pipeline as classify.py, so tagging logic is
identical. This UI covers classification and Excel export; applying tags into
Meltwater is still done by apply_tags.py.
"""

import asyncio
import io
import os
import sys

import pandas as pd
from flask import Flask, jsonify, request, send_file, render_template
from anthropic import AsyncAnthropic, AuthenticationError, APIStatusError

# import the pipeline from the parent package
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
from classify import (
    fetch_full_text, fetch_via_cdp, classify_post, _find_col,
    PERMALINK_HINTS, infer_brand, TOPIC_HINTS,
)
import httpx

app = Flask(__name__, static_folder="static", template_folder="templates")


def run_async(coro):
    """Run an async coroutine from a sync Flask handler (Proactor loop on Windows)."""
    return asyncio.run(coro)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/extract", methods=["POST"])
def extract():
    """Parse an uploaded Excel and return the URLs from its URL column."""
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
def classify():
    data = request.get_json(force=True)
    urls = [u.strip() for u in data.get("urls", []) if u and u.strip()]
    brand = (data.get("brand") or "").strip()
    fetch_mode = data.get("fetch_mode", "cdp")  # 'cdp' or 'anon'
    if not urls:
        return jsonify({"error": "No URLs provided"}), 400
    if not brand:
        return jsonify({"error": "Please specify the run brand (e.g. Kaseya)"}), 400

    try:
        results = run_async(_classify_urls(urls, brand, fetch_mode))
    except AuthenticationError:
        return jsonify({"error": "Invalid or missing ANTHROPIC_API_KEY (check your .env)."}), 400
    except APIStatusError as e:
        msg = str(getattr(e, "message", e))
        if "credit" in msg.lower() or "billing" in msg.lower():
            return jsonify({"error": "Anthropic API has no credit balance — add credits and retry."}), 400
        return jsonify({"error": f"Anthropic API error: {msg}"}), 400
    except Exception as e:
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 500

    return jsonify({"run_brand": brand, "results": results})


async def _classify_urls(urls, brand, fetch_mode):
    posts = [{"permalink": u, "excerpt": ""} for u in urls]

    # 1) fetch full text
    if fetch_mode == "cdp":
        posts = await fetch_via_cdp(posts)
    else:
        sem = asyncio.Semaphore(config.FETCH_CONCURRENCY)
        async with httpx.AsyncClient() as http:
            async def _f(p):
                async with sem:
                    p["text"] = await fetch_full_text(http, p["permalink"], p["excerpt"])
                return p
            posts = await asyncio.gather(*[_f(p) for p in posts])

    # 2) classify in parallel
    anthropic = AsyncAnthropic()
    sem = asyncio.Semaphore(config.CLASSIFY_CONCURRENCY)
    decisions = await asyncio.gather(
        *[classify_post(anthropic, brand, p["permalink"], p.get("text", ""), sem) for p in posts]
    )

    out = []
    for d in decisions:
        tag = d.get("tag") or ""
        sentiment = ""
        if tag and " - " in tag:
            sentiment = tag.split(" - ", 1)[1]
        out.append({
            "permalink": d["permalink"],
            "action": d.get("action"),
            "tag": tag,
            "sentiment": sentiment or ("—" if d.get("action") != "apply" else ""),
            "flag_brand": d.get("flag_brand", ""),
            "reason": d.get("reason", ""),
            "has_text": bool(next((p.get("text") for p in posts if p["permalink"] == d["permalink"]), "")),
        })
    return out


@app.route("/api/export", methods=["POST"])
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
    fname = f"tagging_{brand}.xlsx"
    return send_file(
        buf, as_attachment=True, download_name=fname,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
