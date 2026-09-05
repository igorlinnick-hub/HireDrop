"""Per-user daily usage counters.

Today only tracks `cover_letter_usage` — one shared daily budget for every
paid-AI endpoint (cover letters, screener answers, ATS resume generation).
Single owner of the cover_letter_usage table.

The safe entry point is claim_today()/release_today(): the claim happens in one
SQL statement (claim_ai_use RPC) BEFORE the LLM call, so parallel requests
can't all slip past the check while a generation is in flight.
"""

import contextlib
from datetime import date

from app.db.client import get_supabase

# Passing this as the limit turns claim_today into a pure atomic counter —
# used for admins / observe mode, which must count usage but never block.
COUNT_ONLY_LIMIT = 2_147_483_647


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


def claim_today(user_id: str, limit: int) -> bool:
    """Atomically consume one AI-use slot for today. False = quota exhausted.

    Check-and-increment is ONE statement (claim_ai_use RPC), so the window the
    old flow left open — check, then a seconds-long LLM call, then increment —
    is gone. Falls back to the racey two-step until the migration is applied
    (migrations/add_claim_ai_use.sql); remove the fallback once it's live.
    """
    try:
        res = get_supabase().rpc("claim_ai_use", {"p_user_id": user_id, "p_limit": limit}).execute()
        return bool(res.data)
    except Exception:
        if get_today_count(user_id) >= limit:
            return False
        increment_today(user_id)
        return True


def release_today(user_id: str) -> None:
    """Refund a claimed slot after a generation that FAILED (nothing was spent).

    Best-effort: without it an Anthropic outage would eat the user's whole
    daily quota one error at a time. No-op until the migration is applied.
    """
    with contextlib.suppress(Exception):
        get_supabase().rpc("release_ai_use", {"p_user_id": user_id}).execute()


def admin_ceiling() -> int:
    """Daily AI-call ceiling for accounts that bypass every other limit.

    ADMIN_EMAILS and observe mode skip the tier cap, the per-platform cap and the
    120/day AI cap, which left internal accounts as the only ones that could burn
    Anthropic spend without bound — a forgotten campaign or a test loop ran until
    someone noticed. This is a backstop, not a quota: it sits ~10x above a real
    user's day so testing stays comfortable, and only bites runaway usage.

    ADMIN_AI_DAILY_MAX=0 restores the old unlimited behaviour.
    """
    from config import ADMIN_AI_DAILY_MAX

    return COUNT_ONLY_LIMIT if ADMIN_AI_DAILY_MAX <= 0 else ADMIN_AI_DAILY_MAX


def claim_daily_ai_slot(user_id: str, email: str | None) -> bool:
    """Policy-aware claim shared by paid-AI endpoints outside tools.py.

    Admins and observe mode (RATE_LIMIT_ENFORCE=false) still count usage but
    are never blocked. Imports are local to avoid an import cycle with
    subscriptions/config.
    """
    from app.db.subscriptions import is_admin
    from config import RATE_LIMIT_ENFORCE, RATE_LIMIT_LETTERS_PER_DAY

    if is_admin(email) or not RATE_LIMIT_ENFORCE:
        return claim_today(user_id, admin_ceiling())
    return claim_today(user_id, RATE_LIMIT_LETTERS_PER_DAY)


def increment_today(user_id: str) -> int:
    """Read-then-write upsert — NOT atomic (parallel calls lose updates).

    Kept only as claim_today's pre-migration fallback; don't call directly
    from endpoints — claim BEFORE spending, not after.
    """
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
