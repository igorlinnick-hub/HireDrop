"""Affiliate ledger — commission accrual driven by Stripe money.

Two entry points, both called from the Stripe webhook (app/routers/billing.py):
  invoice.paid    -> accrue_from_invoice()   a paid invoice becomes a commission
  charge.refunded -> reverse_for_invoice()   the customer got the money back

Deliberately NOT driven by signups: a referral that never pays is worth $0, and
paying on registration is how affiliate programs get farmed with dead accounts.

Idempotency lives in the database (commissions.stripe_invoice_id UNIQUE), not
here — Stripe delivers at-least-once and a redelivered invoice.paid must not
accrue twice. Service_role only; migrations/add_affiliates.sql keeps clients out.
"""

import sys
from datetime import UTC, datetime

from app.db.client import get_supabase


def _iso(ts) -> str | None:
    """Stripe unix timestamp -> ISO8601 UTC, or None."""
    if not ts:
        return None
    return datetime.fromtimestamp(int(ts), tz=UTC).isoformat()


def _referral_for_user(user_id: str) -> dict | None:
    """The referral row for a paying user, plus its (active) affiliate."""
    res = (
        get_supabase()
        .table("referrals")
        .select("id, affiliate_id, first_paid_at")
        .eq("referred_user_id", user_id)
        .limit(1)
        .execute()
    )
    return res.data[0] if res.data else None


def _affiliate(affiliate_id: str) -> dict | None:
    res = (
        get_supabase()
        .table("affiliates")
        .select("id, user_id, commission_pct, status")
        .eq("id", affiliate_id)
        .limit(1)
        .execute()
    )
    return res.data[0] if res.data else None


def accrue_from_invoice(user_id: str, invoice: dict) -> None:
    """A referred user paid an invoice — write the affiliate's commission.

    No-ops (silently, by design) when the user wasn't referred, the affiliate is
    not active, or the invoice collected $0 (100% coupon, proration credit).
    """
    invoice_id = invoice.get("id")
    gross = int(invoice.get("amount_paid") or 0)
    if not invoice_id or gross <= 0:
        return

    referral = _referral_for_user(user_id)
    if not referral:
        return

    affiliate = _affiliate(referral["affiliate_id"])
    if not affiliate or affiliate["status"] != "active":
        return
    if affiliate["user_id"] == user_id:
        return  # belt-and-braces; the DB trigger already rejects self-referral

    pct = float(affiliate["commission_pct"])
    commission = round(gross * pct / 100)

    line = ((invoice.get("lines") or {}).get("data") or [{}])[0]
    period = line.get("period") or {}

    try:
        get_supabase().table("commissions").upsert(
            {
                "affiliate_id": affiliate["id"],
                "referral_id": referral["id"],
                "stripe_invoice_id": invoice_id,
                "gross_cents": gross,
                "amount_cents": commission,
                "commission_pct": pct,
                "currency": (invoice.get("currency") or "usd").lower(),
                "period_start": _iso(period.get("start")),
                "period_end": _iso(period.get("end")),
            },
            on_conflict="stripe_invoice_id",
            ignore_duplicates=True,
        ).execute()
    except Exception as e:
        print(f"[affiliate] accrual failed for invoice {invoice_id}: {e}", file=sys.stderr)
        return

    patch = {"status": "paying"}
    if not referral.get("first_paid_at"):
        patch["first_paid_at"] = datetime.now(UTC).isoformat()
    get_supabase().table("referrals").update(patch).eq("id", referral["id"]).execute()


def reverse_for_invoice(invoice_id: str) -> None:
    """Refund clawback: an accrued commission for this invoice is voided.

    A commission already marked paid_out is NOT touched — that money has left
    our PayPal, and taking it back is a conversation, not a database write. It
    gets logged so the monthly ledger review can catch it.
    """
    if not invoice_id:
        return
    try:
        res = (
            get_supabase()
            .table("commissions")
            .select("id, status, affiliate_id, referral_id, amount_cents")
            .eq("stripe_invoice_id", invoice_id)
            .execute()
        )
        for row in res.data or []:
            if row["status"] == "paid_out":
                print(
                    f"[affiliate] refund on ALREADY PAID OUT commission {row['id']} "
                    f"(affiliate {row['affiliate_id']}, {row['amount_cents']}c) — settle by hand",
                    file=sys.stderr,
                )
                continue
            get_supabase().table("commissions").update({"status": "reversed"}).eq(
                "id", row["id"]
            ).execute()
            get_supabase().table("referrals").update({"status": "refunded"}).eq(
                "id", row["referral_id"]
            ).execute()
    except Exception as e:
        print(f"[affiliate] reversal failed for invoice {invoice_id}: {e}", file=sys.stderr)
