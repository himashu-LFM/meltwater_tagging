"""Auth guard: verifies the Supabase access token sent as 'Authorization: Bearer <token>'."""

from functools import wraps

from flask import request, jsonify, g

import db


def require_auth(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not db.is_configured():
            return jsonify({"error": "Server is not configured with Supabase credentials."}), 500
        auth_header = request.headers.get("Authorization", "")
        token = auth_header[7:] if auth_header.startswith("Bearer ") else ""
        user = db.verify_token(token)
        if not user:
            return jsonify({"error": "Not signed in or session expired."}), 401
        g.user = user
        return fn(*args, **kwargs)
    return wrapper
