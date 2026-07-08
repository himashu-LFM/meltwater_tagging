"""
Brand-aware classification for the web app.

Design guarantee: when a brand has NO custom rules and NO custom labels, this
produces byte-for-byte the same prompt and the same tag output as the original
classify_post path — so the ~90% accuracy is untouched. Rules and labels only
*augment*:
  * rules  -> appended as a high-priority section AFTER the base system prompt
  * labels -> only change the final tag STRING, never the judgment
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from prompts import SYSTEM_PROMPT, POST_TEMPLATE, DECISION_SCHEMA
from taxonomy import normalize_brand, SENTIMENTS

_SENT_LOWER = [s.lower() for s in SENTIMENTS]


def build_system_prompt(run_brand: str, rules: dict) -> str:
    """Base prompt + optional brand-specific rules. No rules -> base unchanged."""
    base = SYSTEM_PROMPT.format(run_brand=run_brand)
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
    """Custom label if configured, else the default 'Brand - sentiment' format."""
    return labels.get(sentiment) or f"{run_brand} - {sentiment}"


def resolve(run_brand: str, permalink: str, d: dict, cfg: dict) -> dict:
    """Turn a model decision into a concrete tag or a flag. Mirrors the original
    _resolve logic, but brand validity is scoped to the chosen run brand (so any
    brand works, not just the hardcoded taxonomy list) and uses the brand's own
    roll-up terms in addition to the taxonomy roll-up."""
    action = d.get("action")
    out = {"permalink": permalink, "action": action, "reason": d.get("reason", ""), "tag": None}
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


async def classify_post(anthropic, model, run_brand, permalink, text, sem, cfg):
    system = build_system_prompt(run_brand, cfg.get("rules", {}))
    async with sem:
        try:
            resp = await anthropic.messages.create(
                model=model,
                max_tokens=2000,
                thinking={"type": "adaptive"},
                system=system,
                messages=[{
                    "role": "user",
                    "content": POST_TEMPLATE.format(
                        run_brand=run_brand, permalink=permalink,
                        text=text or "(no text available)",
                    ),
                }],
                output_config={"format": {"type": "json_schema", "schema": DECISION_SCHEMA}},
            )
            raw = next(b.text for b in resp.content if b.type == "text")
            decision = json.loads(raw)
        except Exception as e:
            return {"permalink": permalink, "action": "review",
                    "reason": f"classification error: {e}", "tag": None}
    return resolve(run_brand, permalink, decision, cfg)
