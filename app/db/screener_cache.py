"""Cache for generated screener-question answers.

The fit judge and the cover letter are per-job by nature. Screener questions are
not: "Are you legally authorized to work in the United States?" is the same
question on the hundredth form as on the first, and the same candidate should
give it the same answer. Before this cache, every occurrence was a fresh Sonnet
call — ~28% of the AI cost of an application, spent re-deriving an answer we
already had (see content-lab/campus/ECONOMICS.md §3c).

WHAT IS CACHEABLE, AND WHAT IS NOT
Only CLOSED questions — the ones that arrive with a fixed option set. Their
answer is a pick from that list and does not depend on which company is asking.
Open-ended questions ("Why do you want to work here?") are deliberately NOT
cached: their answer SHOULD differ per job, and serving a stale one would send
the same paragraph about a different employer.

The key also carries a profile fingerprint, so editing the resume or the profile
retires every answer derived from the old one instead of silently reusing it.
"""

import contextlib
import hashlib
import re

from app.db.client import get_supabase

_TABLE = "screener_answer_cache"

# Trailing "*", ":", and the "(required)" marker that ATS forms bolt onto labels —
# the same question renders with and without them on different boards.
_TRIM = re.compile(r"[\s*:：]+$|\(\s*required\s*\)\s*$", re.IGNORECASE)


def _normalise(text: str) -> str:
    """Collapse the cosmetic differences between two renderings of one question.

    Deliberately conservative: case, whitespace and trailing decoration only.
    Stripping words would risk collapsing "3 years of Python" and "3 years of
    Java" into one key, and a wrong cached answer is worse than a paid call.
    """
    cleaned = re.sub(r"\s+", " ", (text or "").strip())
    return _TRIM.sub("", cleaned).strip().lower()


def build_key(question: str, options: list[str] | None, profile: dict) -> str | None:
    """Cache key, or None when this question must not be cached.

    Returns None for open-ended questions (no options) — see the module docstring.
    """
    options = [str(o).strip() for o in (options or []) if str(o).strip()]
    if not options:
        return None

    question_norm = _normalise(question)
    if not question_norm:
        return None

    # Sorted: the same radio group can render in a different order on two boards.
    options_norm = "\x1f".join(sorted(o.lower() for o in options))
    # Any profile edit that could change the right answer must retire the row.
    fingerprint = f"{profile.get('resume_url') or ''}|{profile.get('updated_at') or ''}"

    digest = hashlib.sha256(
        f"{question_norm}\x1e{options_norm}\x1e{fingerprint}".encode()
    ).hexdigest()
    return digest


def get(user_id: str, cache_key: str) -> str | None:
    """Return the cached answer, or None. Never raises — a cache is not a dependency."""
    try:
        res = (
            get_supabase()
            .table(_TABLE)
            .select("answer")
            # user_id is in the filter AND in the primary key: the backend runs as
            # service_role, which bypasses RLS, so scoping is this query's job.
            .eq("user_id", user_id)
            .eq("cache_key", cache_key)
            .limit(1)
            .execute()
        )
        if res.data:
            return res.data[0]["answer"] or None
    except Exception as e:
        print(f"[screener_cache] read failed (answering live): {e}")
    return None


def put(user_id: str, cache_key: str, question: str, answer: str) -> None:
    """Store an answer. Best-effort: a failed write costs one repeat call, nothing more."""
    if not answer:
        return
    try:
        get_supabase().table(_TABLE).upsert(
            {
                "user_id": user_id,
                "cache_key": cache_key,
                "question": question[:600],
                "answer": answer,
            },
            on_conflict="user_id,cache_key",
        ).execute()
    except Exception as e:
        print(f"[screener_cache] write failed (answer still returned): {e}")


def touch(user_id: str, cache_key: str) -> None:
    """Record a hit. Separate from get() so a stats failure can't break answering."""
    # The RPC is a nicety for measuring the hit rate. If the migration that creates
    # it has not been applied yet, answering must carry on regardless.
    with contextlib.suppress(Exception):
        get_supabase().rpc(
            "bump_screener_cache_hit", {"p_user_id": user_id, "p_cache_key": cache_key}
        ).execute()
