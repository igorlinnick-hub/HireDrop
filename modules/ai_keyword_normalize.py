"""AI typo-correction for job-search keywords — the ONE place a typo costs real results.

Keywords flow RAW into board search queries (Indeed q=, LinkedIn keywords=, ZR search=),
so a misspelled term ("ai engeneer") can narrow or misdirect the search. The other AI
layers (fit judge, cover letter, screener answers) read the REAL posting text, so they're
already typo-tolerant — only the SEARCH step needs this.

Conservative by design: fixes only OBVIOUS spelling typos in job titles / skills / tech.
It never rewrites intent, expands abbreviations, or "improves" a valid niche term. It
returns ONLY the terms it changed, so the UI can surface them transparently as a
"did you mean" suggestion the user accepts with one click (never a silent rewrite).

Cheap: one Haiku call for the whole keyword list. See project_unit_economics.
"""

import json

from config import ANTHROPIC_API_KEY
from modules.ai_cover_letter import get_anthropic_client

HAIKU_MODEL = "claude-haiku-4-5-20251001"

_MAX_KEYWORDS = 15
_MAX_KEYWORD_CHARS = 60

_SYSTEM = """You correct OBVIOUS spelling typos in short job-search terms \
(job titles, skills, technologies, industries). You are extremely conservative.

RULES:
- Only fix clear misspellings (e.g. "engeneer"->"engineer", "manger"->"manager", \
"markting"->"marketing", "developr"->"developer").
- NEVER change a correctly-spelled term, even if uncommon or niche.
- NEVER expand abbreviations (keep "AI", "SWE", "PM", "QA", "UX" as-is).
- NEVER change meaning, add words, remove words, or merge/split terms.
- Preserve capitalization style of the input where possible.
- If nothing has a clear typo, return an empty array.

Output ONLY a JSON array, no prose, no code fences. Each element:
{"original": "<input term verbatim>", "suggestion": "<corrected term>"}
Include ONLY terms you actually changed."""


def normalize_keywords(keywords):
    """Return a list of {"original", "suggestion"} for keywords with an obvious typo.

    Returns [] when there's nothing to correct, no API key, or on any failure —
    the caller then searches with the keywords exactly as typed (fail-open: a typo
    is better than a dropped search).
    """
    if not ANTHROPIC_API_KEY:
        return []

    terms = [str(k).strip()[:_MAX_KEYWORD_CHARS] for k in (keywords or []) if str(k).strip()]
    terms = terms[:_MAX_KEYWORDS]
    if not terms:
        return []

    listed = "\n".join(f"- {t}" for t in terms)
    prompt = f"""Correct obvious spelling typos in these job-search terms. \
Return the JSON array as specified — only the terms you changed.

Terms:
{listed}"""

    try:
        client = get_anthropic_client()
        message = client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=400,
            system=_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = (message.content[0].text or "").strip()
    except Exception as e:
        print(f"[keyword_normalize] AI normalization failed: {e}")
        return []

    # Strip accidental code fences, then parse.
    if raw.startswith("```"):
        raw = raw.strip("`")
        raw = raw[raw.find("[") :] if "[" in raw else raw
    try:
        parsed = json.loads(raw)
    except Exception:
        return []
    if not isinstance(parsed, list):
        return []

    # Keep only well-formed, meaningful corrections whose original was actually in the
    # input (guards against the model inventing terms) and that genuinely differ.
    lower_terms = {t.lower(): t for t in terms}
    out = []
    seen = set()
    for item in parsed:
        if not isinstance(item, dict):
            continue
        orig = str(item.get("original", "")).strip()
        sugg = str(item.get("suggestion", "")).strip()[:_MAX_KEYWORD_CHARS]
        if not orig or not sugg:
            continue
        if orig.lower() not in lower_terms:
            continue
        if sugg.lower() == orig.lower():
            continue
        key = orig.lower()
        if key in seen:
            continue
        seen.add(key)
        # Echo the exact original term as it was typed.
        out.append({"original": lower_terms[key], "suggestion": sugg})
    return out
