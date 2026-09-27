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
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.billing_config import PLANS, plan_by_key, tier_for_price
from app.db import affiliates as affiliates_db
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
    plan: str  # "weekly" | "monthly" (keys of billing_config.PLANS)


def _billing_row(user_id: str) -> dict:
    """The user's Stripe ids from their profile ({} if the profile read gives nothing)."""
    from app.db.client import get_supabase

    res = (
        get_supabase()
        .table("profiles")
        .select("stripe_customer_id, stripe_subscription_id")
        .eq("user_id", user_id)
        .execute()
    )
    return (res.data[0] if res.data else {}) or {}


# Statuses that mean "this subscription is still billing them". `past_due` and `unpaid`
# count: Stripe is still retrying the card, so a second checkout would be a second charge.
_LIVE_SUB_STATUSES = ("active", "trialing", "past_due", "unpaid")


def _subscription_is_live(stripe, subscription_id: str) -> bool:
    """Is that subscription still billing? False when Stripe can't tell us — an
    unverifiable answer must not block someone who genuinely needs to subscribe, and
    the customer reuse below keeps both subscriptions reachable from the portal anyway.
    """
    try:
        sub = stripe.Subscription.retrieve(subscription_id)
        if hasattr(sub, "to_dict"):
            sub = sub.to_dict()
        return sub.get("status") in _LIVE_SUB_STATUSES
    except Exception as e:
        print(f"[billing] could not check subscription {subscription_id}: {e}", file=sys.stderr)
        return False


@router.post("/billing/checkout")
def create_checkout(body: CheckoutRequest, user=Depends(get_current_user)):
    """Create a Stripe Checkout Session for the chosen plan; return its URL."""
    stripe = _stripe()
    if stripe is None:
        return JSONResponse(status_code=503, content={"error": "Billing not configured"})

    plan = plan_by_key(body.plan)
    if not plan or not plan["price_id"]:
        return JSONResponse(status_code=400, content={"error": "Unknown or unconfigured plan"})

    # Two guards, both about the same accident: a second checkout by someone who is
    # ALREADY paying. The dashboard hides the plan buttons from subscribers, but it only
    # knows the tier if /stats answered — when that read fails the buttons show, and the
    # old code happily created a SECOND Stripe customer, overwrote stripe_customer_id
    # with it, and left the first subscription invisible to the portal: two charges a
    # month and no way for the user to stop either one (audit 09-25).
    row = _billing_row(user.id)
    customer_id = row.get("stripe_customer_id")
    subscription_id = row.get("stripe_subscription_id")
    if subscription_id and _subscription_is_live(stripe, subscription_id):
        return JSONResponse(
            status_code=409,
            content={
                "error": "already_subscribed",
                "message": (
                    "You already have an active subscription. Open the billing portal to "
                    "switch plans or cancel — a second checkout would charge you twice."
                ),
            },
        )

    # Reuse the customer when we have one: one customer per user is what keeps every
    # past invoice and every subscription in ONE portal session.
    identity = (
        {"customer": customer_id}
        if customer_id
        else {"customer_email": getattr(user, "email", None)}
    )

    try:
        session = stripe.checkout.Session.create(
            mode="subscription",
            line_items=[{"price": plan["price_id"], "quantity": 1}],
            client_reference_id=user.id,
            success_url=f"{FRONTEND_URL}/dashboard?checkout=success",
            cancel_url=f"{FRONTEND_URL}/dashboard/settings?tab=billing&checkout=cancel",
            allow_promotion_codes=True,
            **identity,
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
    """current_period_end (unix) → ISO8601 UTC, matching subscription_expires_at.

    stripe-python v15 pins an API version where current_period_end is gone from the
    subscription's top level and lives on each item instead (live NULL expiry on the
    first real payment, 2026-09-06 — get_tier fail-closed the paid user back to free).
    """
    from datetime import datetime

    ts = subscription.get("current_period_end")
    if not ts:
        items = (subscription.get("items") or {}).get("data") or []
        ts = items[0].get("current_period_end") if items else None
    if not ts:
        return None
    return datetime.fromtimestamp(int(ts), tz=UTC).isoformat()


def _product_of(stripe, price_id: str) -> str | None:
    """The Stripe product a price belongs to (prices carry it either inline or as an id)."""
    price = stripe.Price.retrieve(price_id)
    if hasattr(price, "to_dict"):
        price = price.to_dict()
    product = price.get("product")
    return product if isinstance(product, str) else (product or {}).get("id")


def _tier_for_legacy_price(stripe, price_id: str) -> str | None:
    """A price that is not in the env but IS on our product still grants our paid tier.

    Stripe Prices are IMMUTABLE, so every repricing mints new ids and the env holds only the
    current pair ($12/wk, $39/mo — we already repriced once). A subscriber grandfathered on
    an older price therefore fell through tier_for_price and was never granted anything:
    Stripe kept charging them while get_tier fail-closed them to free at period end. Paying
    and locked out is the worst state the product can put someone in, so instead of guessing
    we ask Stripe whose product the price is — we sell exactly one.
    """
    ours = next((p["price_id"] for p in PLANS.values() if p["price_id"]), None)
    if not ours or not price_id:
        return None
    try:
        if _product_of(stripe, price_id) == _product_of(stripe, ours):
            print(
                f"[billing] price {price_id} is not in env but is OUR product — granting pro. "
                "Add it to STRIPE_PRICE_* (or migrate the subscriber) to stop the lookup.",
                file=sys.stderr,
            )
            return "pro"
    except Exception as e:
        print(f"[billing] product lookup failed for {price_id}: {e}", file=sys.stderr)
    return None


def _grant_from_subscription(stripe, user_id: str, subscription: dict) -> None:
    """Given a Stripe subscription object, grant the matching tier + expiry."""
    items = subscription.get("items", {}).get("data", [])
    price_id = items[0]["price"]["id"] if items else None
    tier = tier_for_price(price_id) or _tier_for_legacy_price(stripe, price_id)
    if not tier:
        print(
            f"[billing] subscription {subscription.get('id')} has unknown price {price_id}",
            file=sys.stderr,
        )
        return
    billing_db.grant(
        user_id, tier, _period_end_iso(subscription), subscription_id=subscription.get("id")
    )


def _resolve_subscription(stripe, invoice_obj: dict, customer_id: str):
    """Get the subscription for an invoice.paid event. On modern Stripe API versions
    the invoice's subscription id isn't always a top-level field, so fall back to the
    invoice line items and finally to the customer's current subscription.
    """
    sub_id = invoice_obj.get("subscription")
    if not sub_id:
        lines = (invoice_obj.get("lines") or {}).get("data") or []
        sub_id = lines[0].get("subscription") if lines else None
    if sub_id:
        try:
            sub = stripe.Subscription.retrieve(sub_id)
            return sub.to_dict() if hasattr(sub, "to_dict") else sub
        except Exception as e:
            print(f"[billing] subscription retrieve failed: {e}", file=sys.stderr)
    try:
        subs = stripe.Subscription.list(customer=customer_id, status="all", limit=1)
        if hasattr(subs, "to_dict"):
            subs = subs.to_dict()
        data = subs.get("data") or []
        return data[0] if data else None
    except Exception as e:
        print(f"[billing] subscription list failed: {e}", file=sys.stderr)
        return None


# A customer we cannot map to a user is usually an ORDERING problem, not a stranger:
# `invoice.paid` can arrive before `checkout.session.completed` has written
# stripe_customer_id (migrations/2026-07-stripe-events.sql warns about exactly this).
# Asking Stripe to retry inside this window is what saves the FIRST invoice's affiliate
# commission — there is no second invoice inside the 60-day attribution window to save it
# later. After the window we stop asking, so a genuinely foreign customer can't keep
# Stripe retrying (and alerting) for days.
UNRESOLVED_RETRY_WINDOW_SECONDS = 24 * 3600


class _UnresolvedCustomerError(Exception):
    """The event names a Stripe customer that maps to no user of ours — yet."""


def _event_age_seconds(event: dict) -> float | None:
    """How long ago Stripe created the event, or None if it didn't say."""
    try:
        return max(0.0, datetime.now(tz=UTC).timestamp() - float(event.get("created")))
    except (TypeError, ValueError):
        return None


def _user_for_customer(etype: str, customer_id: str | None) -> str:
    user_id = billing_db.find_user_by_customer(customer_id) if customer_id else None
    if not user_id:
        raise _UnresolvedCustomerError(
            f"{etype}: customer {customer_id or '(none)'} maps to no user"
        )
    return user_id


def _dispatch_event(stripe, etype: str, obj: dict) -> None:
    """Apply one verified Stripe event. Raises on failure — the caller releases the
    claim and answers 5xx so Stripe re-delivers. Every write in here is idempotent
    (`grant` is an overwrite, commissions upsert on the invoice id), so a retry that
    re-runs a half-finished handler is safe.
    """
    if etype == "checkout.session.completed":
        # First payment. Link customer, then fetch the subscription to grant.
        user_id = obj.get("client_reference_id")
        customer_id = obj.get("customer")
        sub_id = obj.get("subscription")
        if user_id and customer_id:
            billing_db.link_customer(user_id, customer_id)
        if user_id and sub_id:
            subscription = stripe.Subscription.retrieve(sub_id)
            if hasattr(subscription, "to_dict"):
                subscription = subscription.to_dict()
            _grant_from_subscription(stripe, user_id, subscription)

    elif etype in ("customer.subscription.updated", "invoice.paid"):
        # Renewal or plan change. Resolve user via customer id.
        user_id = _user_for_customer(etype, obj.get("customer"))
        sub_obj = (
            obj
            if etype == "customer.subscription.updated"
            else _resolve_subscription(stripe, obj, obj.get("customer"))
        )
        if sub_obj is not None:
            status = sub_obj.get("status")
            if status in ("active", "trialing", "past_due"):
                _grant_from_subscription(stripe, user_id, sub_obj)
            else:
                billing_db.downgrade(user_id)

        # Affiliate commission accrues from COLLECTED money, so it hangs off
        # invoice.paid only — never off signup or subscription.updated.
        if etype == "invoice.paid":
            affiliates_db.accrue_from_invoice(user_id, obj)

    elif etype == "charge.refunded":
        # Clawback: the commission for that invoice is voided. Resolving the
        # user isn't needed — the invoice id alone identifies the commission.
        affiliates_db.reverse_for_invoice(obj.get("invoice"))

    elif etype == "customer.subscription.deleted":
        billing_db.downgrade(_user_for_customer(etype, obj.get("customer")))


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
    # stripe-python v15 StripeObject is NOT a dict — .get() raises (live 500 on the very
    # first real payment, 2026-09-06). Flatten once; everything below is plain dicts.
    if hasattr(event, "to_dict"):
        event = event.to_dict()

    etype = event["type"]
    obj = event["data"]["object"]

    # Idempotency — Stripe delivers at-least-once, so we CLAIM the event id before doing
    # any work and a re-delivery finds it taken. The claim is released if the handler
    # below fails: it used to stand regardless, which turned any single failure into
    # permanent loss — Stripe never retries a 200, and a manual resend lands in this very
    # `duplicate` branch. That is how a paying user stayed `free` (fixable only by editing
    # the database) and how a first invoice's commission vanished (audit 09-25).
    event_id = event.get("id")
    if event_id and not billing_db.claim_event(event_id, etype):
        return {"received": True, "duplicate": True}

    try:
        _dispatch_event(stripe, etype, obj)
    except _UnresolvedCustomerError as exc:
        age = _event_age_seconds(event)
        if age is not None and age > UNRESOLVED_RETRY_WINDOW_SECONDS:
            # Past the point where a retry could help. Keep the claim so Stripe stops
            # re-delivering, and say it loudly — this is a real hole if it ever fires
            # for a customer who DOES have an account.
            print(
                f"[billing] giving up on {event_id} ({exc}) after {age / 3600:.1f}h",
                file=sys.stderr,
            )
            return {"received": True, "unresolved": True}
        print(f"[billing] {exc} — asking Stripe to re-deliver {event_id}", file=sys.stderr)
        if event_id:
            billing_db.release_event(event_id)
        return JSONResponse(status_code=503, content={"error": "unresolved_customer"})
    except Exception as exc:
        # Our own failure on real money. The 5xx (it used to be 200) is what makes Stripe
        # retry, and the released claim is what makes that retry do the work instead of
        # dedup'ing. Stripe backs off over ~3 days, so this is not a hammering risk.
        print(f"[billing] webhook handler error on {etype}: {exc}", file=sys.stderr)
        if event_id:
            billing_db.release_event(event_id)
        return JSONResponse(status_code=500, content={"error": "handler_failed"})

    return {"received": True}
