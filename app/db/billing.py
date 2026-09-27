"""Stripe billing ↔ profiles bridge.

Writes the same `subscription_tier` / `subscription_expires_at` columns the
promo flow uses (so get_tier() is unchanged), plus the Stripe id columns added
in migrations/2026-07-stripe-billing.sql. Service_role only — clients never
touch these.
"""

from app.db.client import get_supabase


def link_customer(user_id: str, customer_id: str) -> None:
    """Record the Stripe customer id for a user (set once at first checkout)."""
    get_supabase().table("profiles").update({"stripe_customer_id": customer_id}).eq(
        "user_id", user_id
    ).execute()


def find_user_by_customer(customer_id: str) -> str | None:
    """Resolve a Stripe customer id back to our user_id (webhook path)."""
    res = (
        get_supabase()
        .table("profiles")
        .select("user_id")
        .eq("stripe_customer_id", customer_id)
        .execute()
    )
    return res.data[0]["user_id"] if res.data else None


def grant(
    user_id: str, tier: str, expires_at: str | None, subscription_id: str | None = None
) -> None:
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
        {
            "subscription_tier": "free",
            "subscription_expires_at": None,
            "stripe_subscription_id": None,
        }
    ).eq("user_id", user_id).execute()


def claim_event(event_id: str, event_type: str) -> bool:
    """Claim a Stripe event for processing. True = we are the first to see it (→ do
    the work), False = someone already has it (→ skip: dedup).

    The row is a CLAIM, not a receipt. Stripe delivers at-least-once, so the claim is
    what stops a re-delivery from double-granting — but it must not outlive a FAILED
    handler, or the event is eaten forever (Stripe never retries a 200, and a manual
    resend lands in the dedup branch). `release_event` is the other half of the pair.

    Fails OPEN (returns True) if the bookkeeping insert errors — better to risk a rare
    re-process (grants and commissions are idempotent) than to drop a real payment event.
    """
    try:
        res = (
            get_supabase()
            .table("stripe_events")
            .upsert(
                {"event_id": event_id, "type": event_type},
                on_conflict="event_id",
                ignore_duplicates=True,
            )
            .execute()
        )
        return bool(res.data)  # non-empty = freshly inserted; empty = duplicate (ignored)
    except Exception as e:
        import sys

        print(f"[billing] event dedup check failed for {event_id}: {e}", file=sys.stderr)
        return True


def release_event(event_id: str) -> None:
    """Drop a claim so a Stripe retry can process the event after all.

    Called when the handler failed. Without it the claim doubled as a receipt: one
    failed handler swallowed the event permanently — the first invoice's affiliate
    commission was lost (no second invoice inside the 60-day attribution window) and a
    payer stayed on `free` until someone edited the database by hand (audit 09-25,
    docs/reviews/2026-09-25-verify-hypotheses.md).
    """
    try:
        get_supabase().table("stripe_events").delete().eq("event_id", event_id).execute()
    except Exception as e:
        import sys

        # The claim stays, so Stripe's retry will dedup and the event is lost. Loud on
        # purpose: this is the one failure here that costs money.
        print(f"[billing] COULD NOT RELEASE event {event_id}: {e}", file=sys.stderr)
