"""Promo-code redemption — grants a paid tier for free, server-side only.

Security: the client never validates or reads codes (the promo_codes table is
RLS-locked to service_role). The client submits a code string; everything below
runs on the backend with the service_role key. A user can never set their own
tier — they can only submit a code that this module validates against the DB.
"""

from datetime import datetime, timedelta, timezone

from app.db.client import get_supabase


def redeem_code(user_id: str, code: str) -> dict:
    """Validate `code` and, if valid, grant its tier to `user_id`.

    Returns a dict: {"ok": bool, "tier"?: str, "expires_at"?: str|None,
    "already"?: bool, "error"?: str}. Idempotent per (user, code): a second
    redemption returns ok without spending another use.
    """
    code = (code or "").strip()
    if not code:
        return {"ok": False, "error": "No code provided"}

    sb = get_supabase()

    # Look up the code (service_role — clients can't do this).
    res = sb.table("promo_codes").select("*").eq("code", code).execute()
    if not res.data:
        return {"ok": False, "error": "This code isn't valid."}
    row = res.data[0]

    if not row.get("active", False):
        return {"ok": False, "error": "This code is no longer active."}

    code_exp = row.get("code_expires_at")
    if code_exp:
        try:
            if datetime.fromisoformat(code_exp.replace("Z", "+00:00")) < datetime.now(timezone.utc):
                return {"ok": False, "error": "This code has expired."}
        except ValueError:
            pass

    tier = row.get("grants_tier") or "elite"

    # Already redeemed by this user? Idempotent — don't spend another use, but
    # RE-APPLY the tier: a first attempt at signup may have logged the redemption
    # before the profiles row existed (update hit 0 rows), so the tier could be
    # unset. Re-asserting here self-heals that without spending another use.
    prior = (
        sb.table("promo_redemptions")
        .select("id, expires_at")
        .eq("user_id", user_id)
        .eq("code", code)
        .execute()
    )
    if prior.data:
        prior_exp = prior.data[0].get("expires_at")
        sb.table("profiles").update(
            {"subscription_tier": tier, "subscription_expires_at": prior_exp}
        ).eq("user_id", user_id).execute()
        return {"ok": True, "tier": tier, "already": True, "expires_at": prior_exp}

    # Atomically claim a use (guards max_uses / active / window under concurrency).
    claimed = sb.rpc("claim_promo_use", {"p_code": code}).execute()
    if not claimed.data:
        return {"ok": False, "error": "This code has been fully used."}

    # Compute per-user access expiry.
    expires_at = None
    duration = row.get("duration_days")
    if duration:
        expires_at = (datetime.now(timezone.utc) + timedelta(days=int(duration))).isoformat()

    # Grant the tier (server-authoritative).
    sb.table("profiles").update(
        {"subscription_tier": tier, "subscription_expires_at": expires_at}
    ).eq("user_id", user_id).execute()

    # Audit log (unique(user_id, code) hard-stops a double redeem).
    try:
        sb.table("promo_redemptions").insert(
            {"user_id": user_id, "code": code, "tier_granted": tier, "expires_at": expires_at}
        ).execute()
    except Exception:
        # Redemption row already exists from a concurrent call — tier is set, fine.
        pass

    return {"ok": True, "tier": tier, "expires_at": expires_at}
