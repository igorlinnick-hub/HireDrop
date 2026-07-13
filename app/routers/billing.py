"""Stripe billing — checkout, webhook, customer portal.

Flow:
  1. Dashboard calls POST /billing/checkout {plan} → we create a Stripe Checkout
     Session and return its URL. client_reference_id carries our user_id.
  2. Stripe redirects the user through payment, then fires webhooks to
     POST /billing/webhook (signature-verified, no auth). We map the paid Price
     → tier and write it to the profile (reusing the promo tier columns).
  3. POST /billing/portal → Stripe Billing Portal URL for cancel / update card
     (this is our one-click cancel path — satisfies FTC click-to-cancel / ARL).

Everything is server-authoritative: the client picks a plan key, never a price
or a tier. Tier is only ever set from a signed Stripe event or the portal.
"""

import sys

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.billing_config import plan_by_key, tier_for_price
from app.db import billing as billing_db
from app.deps import get_current_user
from config import (
    FRONTEND_URL,
    STRIPE_SECRET_KEY,
    STRIPE_WEBHOOK_SECRET,
)

router = APIRouter(tags=["billing"])


def _stripe():
    """Lazy import + configure the Stripe SDK. Returns None if not configured."""
    if not STRIPE_SECRET_KEY:
        return None
    import stripe

    stripe.api_key = STRIPE_SECRET_KEY
    return stripe


class CheckoutRequest(BaseModel):
    plan: str  # "pro" | "premium"


@router.post("/billing/checkout")
def create_checkout(body: CheckoutRequest, user=Depends(get_current_user)):
    """Create a Stripe Checkout Session for the chosen plan; return its URL."""
    stripe = _stripe()
    if stripe is None:
        return JSONResponse(status_code=503, content={"error": "Billing not configured"})

    plan = plan_by_key(body.plan)
    if not plan or not plan["price_id"]:
        return JSONResponse(status_code=400, content={"error": "Unknown or unconfigured plan"})

    try:
        session = stripe.checkout.Session.create(
            mode="subscription",
            line_items=[{"price": plan["price_id"], "quantity": 1}],
            client_reference_id=user.id,
            customer_email=getattr(user, "email", None),
            success_url=f"{FRONTEND_URL}/dashboard?checkout=success",
            cancel_url=f"{FRONTEND_URL}/dashboard/settings?tab=billing&checkout=cancel",
            allow_promotion_codes=True,
            # 3-day free trial: card captured up front, no charge until day 4. Stripe
            # fires subscription.updated with status="trialing" immediately, which the
            # webhook already grants access for — so access starts now, auto-converts to
            # paid on day 4 unless canceled. No custom trial state needed.
            subscription_data={"trial_period_days": 3},
        )
        return {"url": session.url}
    except Exception as e:
        print(f"[billing] checkout create failed: {e}", file=sys.stderr)
        return JSONResponse(status_code=502, content={"error": "Could not start checkout"})


@router.post("/billing/portal")
def create_portal(user=Depends(get_current_user)):
    """Return a Stripe Billing Portal URL (cancel / update payment method)."""
    stripe = _stripe()
    if stripe is None:
        return JSONResponse(status_code=503, content={"error": "Billing not configured"})

    from app.db.client import get_supabase

    res = (
        get_supabase()
        .table("profiles")
        .select("stripe_customer_id")
        .eq("user_id", user.id)
        .execute()
    )
    customer_id = res.data[0]["stripe_customer_id"] if res.data else None
    if not customer_id:
        return JSONResponse(status_code=400, content={"error": "No billing account yet"})

    try:
        portal = stripe.billing_portal.Session.create(
            customer=customer_id,
            return_url=f"{FRONTEND_URL}/dashboard/settings?tab=billing",
        )
        return {"url": portal.url}
    except Exception as e:
        print(f"[billing] portal create failed: {e}", file=sys.stderr)
        return JSONResponse(status_code=502, content={"error": "Could not open billing portal"})


def _period_end_iso(subscription: dict) -> str | None:
    """current_period_end (unix) → ISO8601 UTC, matching subscription_expires_at."""
    from datetime import datetime, timezone

    ts = subscription.get("current_period_end")
    if not ts:
        return None
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()


def _grant_from_subscription(stripe, user_id: str, subscription: dict) -> None:
    """Given a Stripe subscription object, grant the matching tier + expiry."""
    items = subscription.get("items", {}).get("data", [])
    price_id = items[0]["price"]["id"] if items else None
    tier = tier_for_price(price_id)
    if not tier:
        print(f"[billing] subscription {subscription.get('id')} has unknown price {price_id}", file=sys.stderr)
        return
    billing_db.grant(user_id, tier, _period_end_iso(subscription), subscription_id=subscription.get("id"))


@router.post("/billing/webhook")
async def stripe_webhook(request: Request, stripe_signature: str = Header(None)):
    """Handle Stripe events. Signature-verified; no auth dependency by design."""
    stripe = _stripe()
    if stripe is None or not STRIPE_WEBHOOK_SECRET:
        return JSONResponse(status_code=503, content={"error": "Billing not configured"})

    payload = await request.body()
    try:
        event = stripe.Webhook.construct_event(payload, stripe_signature, STRIPE_WEBHOOK_SECRET)
    except Exception as e:
        # Bad signature / malformed — reject without leaking detail.
        print(f"[billing] webhook signature verify failed: {e}", file=sys.stderr)
        return JSONResponse(status_code=400, content={"error": "Invalid signature"})

    etype = event["type"]
    obj = event["data"]["object"]

    try:
        if etype == "checkout.session.completed":
            # First payment. Link customer, then fetch the subscription to grant.
            user_id = obj.get("client_reference_id")
            customer_id = obj.get("customer")
            sub_id = obj.get("subscription")
            if user_id and customer_id:
                billing_db.link_customer(user_id, customer_id)
            if user_id and sub_id:
                subscription = stripe.Subscription.retrieve(sub_id)
                _grant_from_subscription(stripe, user_id, subscription)

        elif etype in ("customer.subscription.updated", "invoice.paid"):
            # Renewal or plan change. Resolve user via customer id.
            sub_obj = obj if etype == "customer.subscription.updated" else None
            customer_id = obj.get("customer")
            user_id = billing_db.find_user_by_customer(customer_id) if customer_id else None
            if not user_id:
                return {"received": True}
            if sub_obj is None:
                sub_id = obj.get("subscription")
                sub_obj = stripe.Subscription.retrieve(sub_id) if sub_id else None
            if sub_obj is not None:
                status = sub_obj.get("status")
                if status in ("active", "trialing", "past_due"):
                    _grant_from_subscription(stripe, user_id, sub_obj)
                else:
                    billing_db.downgrade(user_id)

        elif etype == "customer.subscription.deleted":
            customer_id = obj.get("customer")
            user_id = billing_db.find_user_by_customer(customer_id) if customer_id else None
            if user_id:
                billing_db.downgrade(user_id)

    except Exception as e:
        # Never 500 a webhook on our own logic error — log and ack so Stripe
        # doesn't hammer retries. A missed grant self-heals on the next event.
        print(f"[billing] webhook handler error on {etype}: {e}", file=sys.stderr)

    return {"received": True}
