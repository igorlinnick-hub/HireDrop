"""Subscription tier limits and enforcement for applications.

Free/Pro/Elite tiers — each has its own daily quota for total applications,
plus a per-platform cap that applies regardless of tier (so a power user
can't burn 200 applications into one platform in a day).

Admin tier (env-controlled): emails in `ADMIN_EMAILS` skip all limits.
Pass the user's email through from the router so we can short-circuit before
hitting Supabase.

Mirrors the cover-letter rate limit in app/db/usage.py — same pattern,
different counter table (`applications` instead of `cover_letter_usage`).
"""
from datetime import UTC, datetime

from app.db import applications as apps_db
from app.db.client import get_supabase
from config import ADMIN_EMAILS

TIER_LIMITS = {
    "free": 10,
    "pro": 50,
    "elite": 200,
}

MAX_PER_PLATFORM = 50

# Sentinel for admin "unlimited" so the dashboard renders ∞ instead of a number.
ADMIN_DAILY_LIMIT = 10_000_000


def is_admin(email: str | None) -> bool:
    return bool(email) and email.lower() in ADMIN_EMAILS


def get_tier(user_id: str, email: str | None = None) -> str:
    """Returns the user's active tier.

    Admin emails win first (env-only, no DB lookup needed).
    Expired non-free DB tiers downgrade to free.
    """
    if is_admin(email):
        return "admin"

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
            if exp_dt < datetime.now(UTC):
                return "free"
        except Exception:  # noqa: S110 - invalid timestamps default to current tier; no recovery needed
            pass

    return tier if tier in TIER_LIMITS else "free"


def daily_limit(tier: str) -> int:
    if tier == "admin":
        return ADMIN_DAILY_LIMIT
    return TIER_LIMITS.get(tier, TIER_LIMITS["free"])


def check_can_apply(user_id: str, platform: str, email: str | None = None) -> dict:
    tier = get_tier(user_id, email)

    if tier == "admin":
        # Admins bypass everything — no count, no platform cap.
        return {
            "allowed": True,
            "reason": "",
            "tier": "admin",
            "used_today": apps_db.count_today(user_id),
            "daily_limit": ADMIN_DAILY_LIMIT,
            "platform_used": 0,
        }

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


def get_usage_summary(user_id: str, email: str | None = None) -> dict:
    tier = get_tier(user_id, email)
    used_today = apps_db.count_today(user_id)
    platform_counts = apps_db.count_today_by_platform(user_id)

    if tier == "admin":
        return {
            "tier": "admin",
            "daily_limit": ADMIN_DAILY_LIMIT,
            "used_today": used_today,
            "remaining_today": ADMIN_DAILY_LIMIT,
            "platform_counts": platform_counts,
            "max_per_platform": ADMIN_DAILY_LIMIT,
        }

    limit = daily_limit(tier)
    return {
        "tier": tier,
        "daily_limit": limit,
        "used_today": used_today,
        "remaining_today": max(0, limit - used_today),
        "platform_counts": platform_counts,
        "max_per_platform": MAX_PER_PLATFORM,
    }
