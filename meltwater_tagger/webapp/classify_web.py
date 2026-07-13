"""
Brand-aware classification for the web app.

Design guarantee: brand rules and labels only *augment* the base behaviour:
  * rules  -> appended as a high-priority section AFTER the base system prompt
  * labels -> only change the final tag STRING, never the judgment

Post vs comment: the base post-judging behaviour is preserved unchanged. A
content-type guidance section is always appended so COMMENTS are judged on their
own content (parent post as context only), while POSTS follow the original rules.
"""

import json
import os
import sys

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_THIS_DIR))
sys.path.insert(0, _THIS_DIR)
from prompts import (
    SYSTEM_PROMPT, POST_TEMPLATE, COMMENT_TEMPLATE, CONTENT_TYPE_GUIDANCE,
    DECISION_SCHEMA,
)
from taxonomy import normalize_brand, SENTIMENTS
from logging_setup import get_logger

log = get_logger("classify_web")

_SENT_LOWER = [s.lower() for s in SENTIMENTS]


def build_system_prompt(run_brand: str, rules: dict) -> str:
    """Base prompt + post/comment guidance + optional brand-specific rules.

    The content-type guidance is appended for every run (it's how posts vs
    comments are judged). It preserves post behaviour unchanged and only adds
    explicit comment handling + the reason-prefix requirement."""
    base = SYSTEM_PROMPT.format(run_brand=run_brand) + CONTENT_TYPE_GUIDANCE
    active = [(s, r) for s, r in rules.items() if r and str(r).strip()]
    if not active:
        return base
    lines = [
        "",
        "",
        f"## BRAND-SPECIFIC RULES for {run_brand} (HIGHEST PRIORITY)",
        "Apply these rules when judging this brand. They override or refine the "
        "general guidance above. Where a sentiment has no rule listed here, use "
        "the general guidance as-is.",
    ]
    order = {"positive": 0, "negative": 1, "neutral": 2}
    for s, r in sorted(active, key=lambda x: order.get(x[0], 9)):
        lines.append(f"- {s.upper()}: {str(r).strip()}")
    return base + "\n".join(lines)


def _label_for(run_brand: str, sentiment: str, labels: dict) -> str:
    """Custom label if configured, else the default 'Sentiment - Brand' format
    (confirmed live in Meltwater: 'Negative - Kaseya', 'Neutral - Ninja')."""
    return labels.get(sentiment) or f"{sentiment.capitalize()} - {run_brand}"


def resolve(run_brand: str, permalink: str, d: dict, cfg: dict, content_type: str = "post") -> dict:
    """Turn a model decision into a concrete tag or a flag. Mirrors the original
    _resolve logic, but brand validity is scoped to the chosen run brand (so any
    brand works, not just the hardcoded taxonomy list) and uses the brand's own
    roll-up terms in addition to the taxonomy roll-up."""
    action = d.get("action")
    out = {"permalink": permalink, "action": action, "reason": d.get("reason", ""),
           "tag": None, "content_type": content_type}
    labels = cfg.get("labels", {})
    terms = [t.lower() for t in cfg.get("roll_up_terms", [])]

    if action == "apply":
        primary_raw = (d.get("primary_brand", "") or "").strip()
        sent = (d.get("sentiment", "") or "").lower()
        norm = normalize_brand(primary_raw)  # taxonomy roll-up (Kaseya family, etc.)

        is_run = (
            (norm and norm.lower() == run_brand.lower())
            or primary_raw.lower() == run_brand.lower()
            or any(t in primary_raw.lower() for t in terms)
            or (normalize_brand(run_brand) and norm == normalize_brand(run_brand))
        )

        if is_run and sent in _SENT_LOWER:
            out["tag"] = _label_for(run_brand, sent, labels)
        elif norm and not is_run:
            out["action"] = "skip_flag"
            out["flag_brand"] = norm
            out["reason"] = f"primary brand is {norm}; " + out["reason"]
        else:
            out["action"] = "review"
            out["reason"] = out["reason"] or "could not confirm run brand as primary subject"
    elif action == "skip_flag":
        out["flag_brand"] = normalize_brand(d.get("primary_brand", "")) or d.get("primary_brand", "unknown")
    # review / paywall pass through
    return out


def _build_user_message(run_brand, permalink, text, content_type, post_text, comment_text):
    """Post -> POST_TEMPLATE (post content). Comment -> COMMENT_TEMPLATE with the
    parent post as context and the specific comment as the thing to classify."""
    if content_type == "comment":
        return COMMENT_TEMPLATE.format(
            run_brand=run_brand, permalink=permalink,
            post_text=post_text or "(parent post text unavailable)",
            comment_text=comment_text or text or "(no comment text available)",
        )
    return POST_TEMPLATE.format(
        run_brand=run_brand, permalink=permalink,
        text=(post_text or text or "(no text available)"),
    )


async def classify_post(anthropic, model, run_brand, permalink, text, sem, cfg,
                        content_type="post", post_text="", comment_text=""):
    system = build_system_prompt(run_brand, cfg.get("rules", {}))
    user_msg = _build_user_message(run_brand, permalink, text, content_type, post_text, comment_text)
    async with sem:
        try:
            resp = await anthropic.messages.create(
                model=model,
                max_tokens=2000,
                thinking={"type": "adaptive"},
                system=system,
                messages=[{"role": "user", "content": user_msg}],
                output_config={"format": {"type": "json_schema", "schema": DECISION_SCHEMA}},
            )
            raw = next(b.text for b in resp.content if b.type == "text")
            decision = json.loads(raw)
        except Exception as e:
            log.error("classification failed for %s: %s: %s", permalink, type(e).__name__, e)
            return {"permalink": permalink, "action": "review",
                    "reason": f"classification error: {e}", "tag": None,
                    "content_type": content_type}
    return resolve(run_brand, permalink, decision, cfg, content_type)
