"""Subscription tier limits and enforcement for applications.

Free/Pro/Elite tiers — each has its own daily quota for total applications,
plus a per-platform cap that applies regardless of tier (so a power user
can't burn 200 applications into one platform in a day).

Mirrors the cover-letter rate limit in app/db/usage.py — same pattern,
different counter table (`applications` instead of `cover_letter_usage`).
"""
from datetime import datetime, timezone

from app.db import applications as apps_db
from app.db.client import get_supabase

TIER_LIMITS = {
    "free": 10,
    "pro": 50,
    "elite": 200,
}

MAX_PER_PLATFORM = 50


def get_tier(user_id: str) -> str:
    """Returns the user's active tier. Expired non-free tiers downgrade to free."""
    res = (
        get_supabase()
        .table("profiles")
        .select("subscription_tier, subscription_expires_at")
        .eq("user_id", user_id)
        .execute()
    )
    if not res.data:
        return "free"

    row = res.data[0]
    tier = row.get("subscription_tier") or "free"
    expires = row.get("subscription_expires_at")

    if tier != "free" and expires:
        try:
            exp_dt = datetime.fromisoformat(expires.replace("Z", "+00:00"))
            if exp_dt < datetime.now(timezone.utc):
                return "free"
        except Exception:
            pass

    return tier if tier in TIER_LIMITS else "free"


def daily_limit(tier: str) -> int:
    return TIER_LIMITS.get(tier, TIER_LIMITS["free"])


def check_can_apply(user_id: str, platform: str) -> dict:
    tier = get_tier(user_id)
    limit = daily_limit(tier)
    used_today = apps_db.count_today(user_id)

    if used_today >= limit:
        return {
            "allowed": False,
            "reason": f"Daily limit reached ({limit} applications). Upgrade to apply more.",
            "tier": tier,
            "used_today": used_today,
            "daily_limit": limit,
            "platform_used": 0,
        }

    platform_counts = apps_db.count_today_by_platform(user_id)
    platform_used = platform_counts.get(platform, 0)

    if platform_used >= MAX_PER_PLATFORM:
        return {
            "allowed": False,
            "reason": f"Platform limit reached ({MAX_PER_PLATFORM} applications per platform per day).",
            "tier": tier,
            "used_today": used_today,
            "daily_limit": limit,
            "platform_used": platform_used,
        }

    return {
        "allowed": True,
        "reason": "",
        "tier": tier,
        "used_today": used_today,
        "daily_limit": limit,
        "platform_used": platform_used,
    }


def get_usage_summary(user_id: str) -> dict:
    tier = get_tier(user_id)
    limit = daily_limit(tier)
    used_today = apps_db.count_today(user_id)
    platform_counts = apps_db.count_today_by_platform(user_id)

    return {
        "tier": tier,
        "daily_limit": limit,
        "used_today": used_today,
        "remaining_today": max(0, limit - used_today),
        "platform_counts": platform_counts,
        "max_per_platform": MAX_PER_PLATFORM,
    }
