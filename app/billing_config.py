"""Billing plans — single source of truth for the paid product.

One product (everything included, 30/day), billed WEEKLY or MONTHLY. No free tier,
no trial, no annual. Both cadences grant the same tier — the only difference is the
Stripe Price / billing interval. Keep in sync with TIER_LIMITS in
app/db/subscriptions.py and the frontend pricing.
"""

from config import STRIPE_PRICE_MONTHLY, STRIPE_PRICE_WEEKLY

# plan key (what the checkout endpoint accepts) → plan metadata.
# Both grant tier "pro" (the full paid product) — cadence is the only difference.
PLANS = {
    "weekly": {
        "tier": "pro",
        "name": "Weekly",
        "price_usd": 9,
        "interval": "week",
        "price_id": STRIPE_PRICE_WEEKLY,
    },
    "monthly": {
        "tier": "pro",
        "name": "Monthly",
        "price_usd": 29,
        "interval": "month",
        "price_id": STRIPE_PRICE_MONTHLY,
    },
}


def plan_by_key(key: str) -> dict | None:
    """Return the plan for a checkout request key ('weekly' | 'monthly')."""
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
