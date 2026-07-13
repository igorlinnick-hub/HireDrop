"""Per-user daily usage counters.

Today only tracks `cover_letter_usage` — increment on every successful
cover-letter generation, read for quota check before the LLM call.
Single owner of the cover_letter_usage table.
"""

from datetime import date

from app.db.client import get_supabase


def get_today_count(user_id: str) -> int:
    today = date.today().isoformat()
    res = (
        get_supabase()
        .table("cover_letter_usage")
        .select("count")
        .eq("user_id", user_id)
        .eq("date", today)
        .execute()
    )
    return res.data[0]["count"] if res.data else 0


def increment_today(user_id: str) -> int:
    """Atomic upsert: +1 to today's row. Returns the new count."""
    today = date.today().isoformat()
    current = get_today_count(user_id)
    new_count = current + 1
    (
        get_supabase()
        .table("cover_letter_usage")
        .upsert(
            {"user_id": user_id, "date": today, "count": new_count},
            on_conflict="user_id,date",
        )
        .execute()
    )
    return new_count


def over_daily_ai_limit(user_id: str, email: str | None) -> bool:
    """True when the user has hit the daily AI quota and enforcement is on.

    Shared by every paid-AI endpoint (cover letters, screener answers, ATS resume
    generation) so they all draw from one daily budget. Admins are never limited.
    Imports are local to avoid an import cycle with subscriptions/config.
    """
    from config import RATE_LIMIT_ENFORCE, RATE_LIMIT_LETTERS_PER_DAY

    from app.db.subscriptions import is_admin

    if is_admin(email):
        return False
    return RATE_LIMIT_ENFORCE and get_today_count(user_id) >= RATE_LIMIT_LETTERS_PER_DAY
