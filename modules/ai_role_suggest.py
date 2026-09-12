"""Propose the job titles a candidate can realistically search for, read off their resume.

A mode that says "pick the roles you want" is only as good as what the user types into an
empty field. The address field is the cautionary tale: a blank input nobody could answer
off the top of their head was filled by 1 user in 28. Roles are worse than an address —
a wrong guess is invisible, it just returns the wrong industry forever. Igor's own account
searched "ai engineer" + "project manager" against a marketing resume for weeks and the
judge honestly skipped every construction PM it found.

So the field is never blank: the resume the user already uploaded proposes the roles and
they tick the ones they want. Haiku, one call, the same cost lever as keyword typo fixes.

How many they may tick is the apply mode's job — see ROLE_LIMITS. That is the whole
meaning of broad-vs-precise stated in something a person can see and change, instead of
a hidden 35/55/70 fit threshold.
"""

import json

from config import ANTHROPIC_API_KEY
from modules.ai_cover_letter import get_anthropic_client

HAIKU_MODEL = "claude-haiku-4-5-20251001"

# How many roles each apply mode may carry. Broad casts a wide net across adjacent titles;
# precise means "these three and nothing else". The numbers are a product decision
# (Igor, 2026-09-11), and they are served to the UI so the limit is stated in ONE place.
ROLE_LIMITS = {"broad": 7, "standard": 5, "precise": 3}
DEFAULT_ROLE_LIMIT = ROLE_LIMITS["standard"]
_MAX_SUGGESTIONS = max(ROLE_LIMITS.values())
_MAX_RESUME_CHARS = 3000

_SYSTEM = """You turn a resume into job-board SEARCH PHRASES — the words this person would \
type into Indeed to find work they can actually get.

RULES:
- Only titles the resume SUPPORTS. A marketing resume never suggests "software engineer".
- Real job titles as boards spell them ("social media manager", "marketing coordinator"), \
2-3 words, lowercase, no seniority inflation, no company names, no skills ("figma" is not a role).
- Cover the realistic range: the title they held, adjacent titles at the same level, and the \
obvious next step — not seven spellings of one job.
- Order by how likely this candidate is to be hired for it, most likely first.
- No duplicates, no near-duplicates ("social media manager" and "manager social media").

Output ONLY a JSON array of strings, no prose, no code fences."""


def role_limit(apply_mode: str | None) -> int:
    """Roles allowed in this mode. An unknown mode gets the middle limit, never the widest."""
    return ROLE_LIMITS.get((apply_mode or "").strip().lower(), DEFAULT_ROLE_LIMIT)


def suggest_roles(resume_text: str | None, limit: int = _MAX_SUGGESTIONS) -> list[str]:
    """Roles read off the resume, best first. [] when there is nothing to read.

    Returns empty rather than inventing: a suggestion with no resume behind it is exactly
    the blind guess this exists to replace, and the caller can tell the user to upload a
    resume first.
    """
    text = (resume_text or "").strip()
    if not text or not ANTHROPIC_API_KEY:
        return []

    try:
        client = get_anthropic_client()
        message = client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=300,
            system=_SYSTEM,
            messages=[
                {"role": "user", "content": f"<resume>\n{text[:_MAX_RESUME_CHARS]}\n</resume>"}
            ],
        )
        raw = (message.content[0].text or "").strip()
        start, end = raw.find("["), raw.rfind("]")
        data = json.loads(raw[start : end + 1]) if start != -1 and end != -1 else []
    except Exception as e:
        print(f"[role_suggest] failed: {e}")
        return []

    out: list[str] = []
    seen: set[str] = set()
    for item in data if isinstance(data, list) else []:
        role = " ".join(str(item).split()).strip().lower()[:60]
        if not role or role in seen:
            continue
        seen.add(role)
        out.append(role)
    return out[: max(1, min(int(limit or _MAX_SUGGESTIONS), _MAX_SUGGESTIONS))]
