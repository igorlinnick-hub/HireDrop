"""Stripe billing ↔ profiles bridge.

Writes the same `subscription_tier` / `subscription_expires_at` columns the
promo flow uses (so get_tier() is unchanged), plus the Stripe id columns added
in migrations/2026-07-stripe-billing.sql. Service_role only — clients never
touch these.
"""

from typing import Optional

from app.db.client import get_supabase


def link_customer(user_id: str, customer_id: str) -> None:
    """Record the Stripe customer id for a user (set once at first checkout)."""
    get_supabase().table("profiles").update(
        {"stripe_customer_id": customer_id}
    ).eq("user_id", user_id).execute()


def find_user_by_customer(customer_id: str) -> Optional[str]:
    """Resolve a Stripe customer id back to our user_id (webhook path)."""
    res = (
        get_supabase()
        .table("profiles")
        .select("user_id")
        .eq("stripe_customer_id", customer_id)
        .execute()
    )
    return res.data[0]["user_id"] if res.data else None


def grant(user_id: str, tier: str, expires_at: Optional[str], subscription_id: Optional[str] = None) -> None:
    """Grant/refresh a paid tier. `expires_at` = Stripe current_period_end (ISO).

    get_tier() downgrades to free automatically once expires_at passes, so a
    lapsed renewal needs no extra webhook to take effect — but we also handle
    subscription.deleted explicitly via downgrade() for immediacy.
    """
    patch = {"subscription_tier": tier, "subscription_expires_at": expires_at}
    if subscription_id:
        patch["stripe_subscription_id"] = subscription_id
    get_supabase().table("profiles").update(patch).eq("user_id", user_id).execute()


def downgrade(user_id: str) -> None:
    """Drop to free (subscription canceled/expired). Keeps customer id for reuse."""
    get_supabase().table("profiles").update(
        {"subscription_tier": "free", "subscription_expires_at": None, "stripe_subscription_id": None}
    ).eq("user_id", user_id).execute()
