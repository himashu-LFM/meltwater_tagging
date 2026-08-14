"""
Turn an uploaded client feedback doc into discrete, reusable tagging rules.

The client's "Tagging Adjustments" docs are per-article corrections ("this URL
should be X, not Y, because…"). We ask Claude to distil each correction into a
GENERAL rule the classifier can apply to future articles — plus the example URL
(kept for the test set), and a category so rules can be grouped in the prompt.

Used by the upload endpoint: doc saved → extract_rules(text) → store each rule
in feedback_rules. This is what makes the feedback loop DB-backed and automatic.
"""

import json

from anthropic import Anthropic

import config

CATEGORIES = [
    "scope", "region", "coverage", "publication",
    "pillar", "product", "industry", "corporate", "spokesperson", "general",
]

SYSTEM_PROMPT = """You extract reusable media-tagging RULES from a client's feedback document for \
Bentley Systems media monitoring.

The document contains per-article corrections an analyst received (e.g. "remove Pillar - AI, this is \
Connected Data because…", "this source is not in scope", "region should be by publication country"). \
Your job: turn these into GENERAL, reusable rules a classifier can apply to FUTURE articles.

Rules:
- GENERALISE. Do not write "this URL should be X". Write the underlying principle, e.g. \
"When an event/conference article only lists AI as a theme without substantive AI discussion, do NOT apply Pillar - Infrastructure AI."
- One rule per distinct instruction. Merge duplicates.
- If a specific example article/URL is present, capture it in example_url (for QA testing). Otherwise leave it empty.
- Categorise each rule as one of: scope, region, coverage, publication, pillar, product, industry, corporate, spokesperson, general.
- Ignore anything that is not a tagging instruction (greetings, signatures, filenames).
- If the document contains no usable rules, return an empty list."""

USER_TEMPLATE = """Feedback document text:

--- BEGIN ---
{text}
--- END ---

Extract the reusable tagging rules as structured output."""

SCHEMA = {
    "type": "object",
    "properties": {
        "rules": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "category": {"type": "string", "enum": CATEGORIES},
                    "rule_text": {"type": "string", "description": "The general, reusable instruction."},
                    "example_url": {"type": "string", "description": "Source article URL if present, else empty."},
                },
                "required": ["category", "rule_text"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["rules"],
    "additionalProperties": False,
}


# A single doc can contain dozens of corrections. Extracting them all in one
# call blows the output-token budget (thinking + long JSON). So we split the doc
# into line-bounded chunks, extract each, then merge + de-duplicate. This scales
# to arbitrarily large docs and keeps every call small and fast.
CHUNK_CHARS = 9000


def _chunk(text: str, size: int = CHUNK_CHARS) -> list[str]:
    chunks, cur = [], ""
    for line in text.split("\n"):
        if cur and len(cur) + len(line) + 1 > size:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    if cur.strip():
        chunks.append(cur)
    return chunks or [text]


def _extract_chunk(client: Anthropic, chunk: str) -> list[dict]:
    resp = client.messages.create(
        model=config.MODEL,
        max_tokens=8000,
        thinking={"type": "adaptive"},
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": USER_TEMPLATE.format(text=chunk)}],
        output_config={"format": {"type": "json_schema", "schema": SCHEMA}},
    )
    raw = next((b.text for b in resp.content if b.type == "text"), None)
    if raw is None:
        raise RuntimeError(f"no rules returned (stop_reason={resp.stop_reason})")
    out = []
    for r in json.loads(raw).get("rules", []):
        text = (r.get("rule_text") or "").strip()
        if not text:
            continue
        cat = (r.get("category") or "general").strip().lower()
        if cat not in CATEGORIES:
            cat = "general"
        out.append({
            "category": cat,
            "rule_text": text,
            "example_url": (r.get("example_url") or "").strip() or None,
        })
    return out


def extract_rules(doc_text: str) -> list[dict]:
    """Return a list of {category, rule_text, example_url} extracted from the doc.

    Chunks large docs to stay within the output-token budget. Raises only if
    EVERY chunk fails; partial-chunk failures are skipped so one bad section
    doesn't lose the rest. The uploaded doc itself is saved regardless.
    """
    if not doc_text or not doc_text.strip():
        return []

    client = Anthropic()  # ANTHROPIC_API_KEY from env
    chunks = _chunk(doc_text)
    all_rules, errors = [], 0
    for ch in chunks:
        try:
            all_rules.extend(_extract_chunk(client, ch))
        except Exception:
            errors += 1
    if errors and errors == len(chunks):
        raise RuntimeError("rule extraction failed for all chunks")

    # de-duplicate by normalised rule text (first occurrence wins)
    seen, deduped = set(), []
    for r in all_rules:
        key = r["rule_text"].strip().lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(r)
    return deduped
