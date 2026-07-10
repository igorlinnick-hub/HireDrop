"""Billing plans — single source of truth for paid tiers.

Maps a plan the frontend can request → the tier string we grant → the Stripe
Price object that charges for it. Keep this in sync with the tier limits in
app/db/subscriptions.py (TIER_LIMITS) and the frontend pricing page.

Free is not here — it's the default when no paid subscription is active.
"""

from config import STRIPE_PRICE_PREMIUM, STRIPE_PRICE_PRO

# plan key (what the checkout endpoint accepts) → plan metadata
PLANS = {
    "pro": {
        "tier": "pro",
        "name": "Pro",
        "price_usd": 19,
        "price_id": STRIPE_PRICE_PRO,
    },
    "premium": {
        "tier": "premium",
        "name": "Premium",
        "price_usd": 29,
        "price_id": STRIPE_PRICE_PREMIUM,
    },
}


def plan_by_key(key: str) -> dict | None:
    """Return the plan for a checkout request key ('pro' | 'premium')."""
    return PLANS.get((key or "").lower())


def tier_for_price(price_id: str) -> str | None:
    """Reverse-map a Stripe Price ID (from a webhook line item) → our tier.

    Returns None if the price isn't one of ours (ignore the event).
    """
    if not price_id:
        return None
    for plan in PLANS.values():
        if plan["price_id"] and plan["price_id"] == price_id:
            return plan["tier"]
    return None
