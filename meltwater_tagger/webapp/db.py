"""
Supabase data access for the web app: auth verification, brand config,
per-user Meltwater/Reddit credentials, and run history.

Requires env vars (see .env.example):
  SUPABASE_URL, SUPABASE_ANON_KEY, SUPABASE_SERVICE_ROLE_KEY

The service_role key is used SERVER-SIDE ONLY (never sent to the browser) so
the backend can read/write any user's row while still scoping every query by
user_id explicitly, on top of the Row Level Security policies in schema.sql.
"""

import os

from supabase import create_client, Client

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_ANON_KEY = os.environ.get("SUPABASE_ANON_KEY", "")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")

_configured = bool(SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY)

_client: Client | None = None


def is_configured() -> bool:
    return _configured


def get_client() -> Client:
    """Server-side client using the service_role key (bypasses RLS by design;
    every function below scopes by user_id explicitly)."""
    global _client
    if not _configured:
        raise RuntimeError(
            "Supabase is not configured. Set SUPABASE_URL and "
            "SUPABASE_SERVICE_ROLE_KEY (see .env.example)."
        )
    if _client is None:
        _client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)
    return _client


def verify_token(access_token: str):
    """Validate a Supabase Auth access token (sent by the frontend after
    login) and return the user object, or None if invalid/expired."""
    if not access_token:
        return None
    try:
        resp = get_client().auth.get_user(access_token)
        return resp.user
    except Exception:
        return None


# --- Brands -----------------------------------------------------------------

def list_brands() -> list[dict]:
    r = get_client().table("brands").select("*").eq("active", True).order("name").execute()
    return r.data or []


def get_brand(name: str) -> dict | None:
    r = get_client().table("brands").select("*").ilike("name", name).limit(1).execute()
    return r.data[0] if r.data else None


def upsert_brand(name: str, roll_up_terms: list[str] | None = None,
                  meltwater_topic_url: str | None = None) -> dict:
    payload = {"name": name}
    if roll_up_terms is not None:
        payload["roll_up_terms"] = roll_up_terms
    if meltwater_topic_url is not None:
        payload["meltwater_topic_url"] = meltwater_topic_url
    r = get_client().table("brands").upsert(payload, on_conflict="name").execute()
    return r.data[0] if r.data else payload


def update_brand(brand_id: int, name: str | None = None, roll_up_terms: list[str] | None = None,
                  meltwater_topic_url: str | None = None) -> dict:
    """Update an existing brand by id (lets you rename or change its topic URL)."""
    payload = {}
    if name is not None:
        payload["name"] = name
    if roll_up_terms is not None:
        payload["roll_up_terms"] = roll_up_terms
    if meltwater_topic_url is not None:
        payload["meltwater_topic_url"] = meltwater_topic_url
    if not payload:
        return {}
    r = get_client().table("brands").update(payload).eq("id", brand_id).execute()
    return r.data[0] if r.data else payload


def delete_brand(brand_id: int):
    get_client().table("brands").delete().eq("id", brand_id).execute()


# --- Per-user topic URL override ---------------------------------------------

def get_user_topic_url(user_id: str, brand_id: int) -> str | None:
    r = (get_client().table("user_brand_topics").select("topic_url")
         .eq("user_id", user_id).eq("brand_id", brand_id).limit(1).execute())
    return r.data[0]["topic_url"] if r.data else None


def upsert_user_topic_url(user_id: str, brand_id: int, topic_url: str):
    get_client().table("user_brand_topics").upsert(
        {"user_id": user_id, "brand_id": brand_id, "topic_url": topic_url},
        on_conflict="user_id,brand_id",
    ).execute()


def resolve_topic_url(user_id: str, brand: dict) -> str | None:
    """A user's own topic URL for this brand takes priority over the shared
    org default, since Meltwater accounts often have differently-named saved
    searches for the same brand."""
    override = get_user_topic_url(user_id, brand["id"])
    return override or brand.get("meltwater_topic_url")


# --- Per-brand tag list + rules ----------------------------------------------

def get_brand_tags(brand_id: int) -> list[dict]:
    r = (get_client().table("brand_tags").select("*")
         .eq("brand_id", brand_id).order("sentiment").execute())
    return r.data or []


def upsert_brand_tag(brand_id: int, sentiment: str, tag_label: str, rule: str | None):
    get_client().table("brand_tags").upsert(
        {"brand_id": brand_id, "sentiment": sentiment, "tag_label": tag_label, "rule": rule},
        on_conflict="brand_id,sentiment",
    ).execute()


def brand_config(brand_name: str) -> dict:
    """Assemble the classification config for a brand: labels + rules + roll-up
    terms. Returns empty structures when nothing is configured (default path)."""
    brand = get_brand(brand_name)
    if not brand:
        return {"labels": {}, "rules": {}, "roll_up_terms": []}
    tags = get_brand_tags(brand["id"])
    return {
        "labels": {t["sentiment"]: t["tag_label"] for t in tags if t.get("tag_label")},
        "rules": {t["sentiment"]: t.get("rule") for t in tags if t.get("rule")},
        "roll_up_terms": brand.get("roll_up_terms") or [],
    }


# --- Meltwater credentials ----------------------------------------------------

def get_meltwater_creds(user_id: str) -> dict | None:
    r = (get_client().table("meltwater_credentials")
         .select("meltwater_email, updated_at")  # never return the password to the client
         .eq("user_id", user_id).limit(1).execute())
    return r.data[0] if r.data else None


def get_meltwater_creds_full(user_id: str) -> dict | None:
    """Server-internal use only (e.g. the apply-to-Meltwater job) — includes password."""
    r = (get_client().table("meltwater_credentials").select("*")
         .eq("user_id", user_id).limit(1).execute())
    return r.data[0] if r.data else None


def upsert_meltwater_creds(user_id: str, email: str, password: str | None):
    payload = {"user_id": user_id, "meltwater_email": email}
    if password:  # allow updating just the email without re-entering password
        payload["meltwater_password"] = password
    get_client().table("meltwater_credentials").upsert(payload, on_conflict="user_id").execute()


# --- Meltwater Auth0 session (Local Storage token cache) --------------------

def get_meltwater_session(user_id: str) -> str | None:
    r = (get_client().table("meltwater_sessions").select("storage_value")
         .eq("user_id", user_id).limit(1).execute())
    return r.data[0]["storage_value"] if r.data else None


def get_meltwater_session_meta(user_id: str) -> dict | None:
    r = (get_client().table("meltwater_sessions").select("updated_at")
         .eq("user_id", user_id).limit(1).execute())
    return r.data[0] if r.data else None


def upsert_meltwater_session(user_id: str, storage_value: str):
    get_client().table("meltwater_sessions").upsert(
        {"user_id": user_id, "storage_value": storage_value}, on_conflict="user_id"
    ).execute()


# --- Reddit session cookie ----------------------------------------------------

def get_reddit_session(user_id: str) -> dict | None:
    r = (get_client().table("reddit_sessions").select("updated_at")
         .eq("user_id", user_id).limit(1).execute())
    return r.data[0] if r.data else None


def get_reddit_cookie(user_id: str) -> str | None:
    r = (get_client().table("reddit_sessions").select("cookie_value")
         .eq("user_id", user_id).limit(1).execute())
    return r.data[0]["cookie_value"] if r.data else None


def upsert_reddit_cookie(user_id: str, cookie_value: str):
    get_client().table("reddit_sessions").upsert(
        {"user_id": user_id, "cookie_value": cookie_value}, on_conflict="user_id"
    ).execute()


# --- Run history --------------------------------------------------------------

def save_run(user_id: str, brand_name: str, results: list[dict], status: str = "classified") -> dict:
    counts = {"positive": 0, "negative": 0, "neutral": 0, "flagged": 0, "applied": 0}
    for r in results:
        s = (r.get("sentiment") or "").lower()
        if s in ("positive", "negative", "neutral"):
            counts[s] += 1
        else:
            counts["flagged"] += 1
        if r.get("action") == "apply":
            counts["applied"] += 1

    brand = get_brand(brand_name)
    payload = {
        "user_id": user_id,
        "brand_id": brand["id"] if brand else None,
        "brand_name": brand_name,
        "status": status,
        "total_posts": len(results),
        "applied_count": counts["applied"],
        "negative_count": counts["negative"],
        "positive_count": counts["positive"],
        "neutral_count": counts["neutral"],
        "flagged_count": counts["flagged"],
        "results": results,
    }
    r = get_client().table("tagging_runs").insert(payload).execute()
    return r.data[0] if r.data else payload


def update_run_status(run_id: str, status: str):
    get_client().table("tagging_runs").update({"status": status}).eq("id", run_id).execute()


def update_run_after_apply(run_id: str, results: list[dict], status: str):
    """Persist the per-post 'applied' flags (set by the caller from the real
    apply report) alongside the run status, so History always reflects what
    Meltwater actually confirmed -- not just that the button was clicked."""
    get_client().table("tagging_runs").update(
        {"status": status, "results": results}
    ).eq("id", run_id).execute()


def delete_run(user_id: str, run_id: str):
    get_client().table("tagging_runs").delete().eq("user_id", user_id).eq("id", run_id).execute()


def list_runs(user_id: str, limit: int = 50) -> list[dict]:
    r = (get_client().table("tagging_runs")
         .select("id, brand_name, status, total_posts, applied_count, "
                 "negative_count, positive_count, neutral_count, flagged_count, created_at")
         .eq("user_id", user_id).order("created_at", desc=True).limit(limit).execute())
    return r.data or []


def get_run(user_id: str, run_id: str) -> dict | None:
    r = (get_client().table("tagging_runs").select("*")
         .eq("user_id", user_id).eq("id", run_id).limit(1).execute())
    return r.data[0] if r.data else None
