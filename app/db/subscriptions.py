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
from datetime import datetime, timezone
from typing import Optional

from config import ADMIN_EMAILS

from app.db import applications as apps_db
from app.db.client import get_supabase

TIER_LIMITS = {
    "free": 10,
    "pro": 30,      # paid daily cap — 30/day × $0.03 ≈ $27/mo keeps a maxed user profitable at $29/mo
    "premium": 30,  # same volume as pro; premium's differentiator is ATS resume tailoring, not quota
    "elite": 200,   # legacy tier, not sold
}

# Ban-safety cap: bans are counted PER platform, so 20/day/platform keeps each
# account looking human. Volume scales by breadth (more platforms), not depth on one.
# THIS IS THE SINGLE SOURCE OF TRUTH. GET /campaign/status serves this number (+ the
# tier's daily_limit) to the extension at campaign start; content.js/background.js read
# it into campaignCaps and enforce it PRE-submit. No more hand-aligned hardcodes — to
# change the cap, edit this one value (and TIER_LIMITS above for the daily budget).
MAX_PER_PLATFORM = 20

# Tap-mode daily cap (PLAN_VOLUME_CAPTCHA_ECONOMICS.md §3): when the user runs "tap" mode
# they review + edit every cover letter before submit, so we generate with the ~10× cheaper
# model (see modules/ai_cover_letter) and a higher volume stays profitable. Paid tiers only.
TAP_DAILY_LIMIT = 100

# Sentinel for admin "unlimited" so the dashboard renders ∞ instead of a number.
ADMIN_DAILY_LIMIT = 10_000_000


def get_submit_mode(user_id: str) -> str:
    """'auto' (default) | 'tap'. Absent/error → 'auto' (backward-compatible)."""
    try:
        res = get_supabase().table("profiles").select("submit_mode").eq("user_id", user_id).execute()
        if res.data:
            return (res.data[0].get("submit_mode") or "auto").lower()
    except Exception:
        pass
    return "auto"


def is_admin(email: Optional[str]) -> bool:
    return bool(email) and email.lower() in ADMIN_EMAILS


def get_tier(user_id: str, email: Optional[str] = None) -> str:
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

    if tier != "free":
        # A paid tier MUST have a valid, future expiry. Missing or unparseable → fail
        # CLOSED (treat as expired) so a NULL/malformed timestamp can't grant paid forever.
        if not expires:
            return "free"
        try:
            exp_dt = datetime.fromisoformat(expires.replace("Z", "+00:00"))
        except Exception:
            return "free"
        if exp_dt < datetime.now(timezone.utc):
            return "free"

    return tier if tier in TIER_LIMITS else "free"


def daily_limit(tier: str, submit_mode: str = "auto") -> int:
    if tier == "admin":
        return ADMIN_DAILY_LIMIT
    base = TIER_LIMITS.get(tier, TIER_LIMITS["free"])
    # Tap mode lifts the cap for PAID tiers only (free stays free-tier limited even in tap).
    if submit_mode == "tap" and tier in ("pro", "premium", "elite"):
        return max(TAP_DAILY_LIMIT, base)
    return base


def check_can_apply(user_id: str, platform: str, email: Optional[str] = None) -> dict:
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

    limit = daily_limit(tier, get_submit_mode(user_id))
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


def get_usage_summary(user_id: str, email: Optional[str] = None) -> dict:
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

    submit_mode = get_submit_mode(user_id)
    limit = daily_limit(tier, submit_mode)
    return {
        "tier": tier,
        "daily_limit": limit,
        "used_today": used_today,
        "remaining_today": max(0, limit - used_today),
        "platform_counts": platform_counts,
        "max_per_platform": MAX_PER_PLATFORM,
        "submit_mode": submit_mode,
    }
