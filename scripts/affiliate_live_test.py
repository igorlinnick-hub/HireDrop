#!/usr/bin/env python3
"""The referral money path, checked against production before and after a real payment.

Everything in the chain is proven EXCEPT the one link that matters: no real
Stripe payment has ever travelled a real referral link. The same gap at the
checkout shipped two production bugs with green mocks (#145, #147), so this
script exists to make the live run cheap to do and impossible to misread.

  before:  .venv/bin/python scripts/affiliate_live_test.py preflight <code>
  during:  pay with a card on an account created through hiredrop.io/?ref=<code>
  after:   .venv/bin/python scripts/affiliate_live_test.py verify <code>

`verify` walks the chain link by link and names the FIRST one that is broken —
attribution, referral, commission — because "no commission" has four different
causes that need four different fixes.

Read-only: it never writes, so it can be run as often as you like mid-test.
"""

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402,F401 — importing it loads .env
from app.db.client import get_supabase  # noqa: E402

FRONTEND = getattr(config, "FRONTEND_URL", "https://hiredrop.io")
OK, BAD, MEH = "PASS", "FAIL", "····"


def money(cents: int) -> str:
    return f"${cents / 100:,.2f}"


def line(state: str, text: str) -> None:
    print(f"  [{state}] {text}")


def affiliate_row(code: str) -> dict | None:
    res = (
        get_supabase()
        .table("affiliates")
        .select("id, user_id, code, status, commission_pct, paypal_email")
        .eq("code", code)
        .limit(1)
        .execute()
    )
    return res.data[0] if res.data else None


def cmd_preflight(args) -> None:
    code = args.code.strip().lower()
    db = get_supabase()
    print(f"\nPreflight for ?ref={code}\n")

    aff = affiliate_row(code)
    if not aff:
        line(BAD, f"No affiliate with code '{code}'. Issue one first:")
        print(f"        .venv/bin/python scripts/affiliate_admin.py issue <email> --code {code}")
        sys.exit(1)

    line(OK if aff["status"] == "active" else BAD, f"affiliate row: status={aff['status']}, rate={aff['commission_pct']}%")
    if aff["status"] != "active":
        # Only 'active' codes are matched by the attribution trigger — a pending
        # or banned partner produces a signup with no referral row at all.
        line(BAD, "Only an ACTIVE affiliate gets credited. Fix before testing.")
        sys.exit(1)

    clicks = db.table("affiliate_clicks").select("id", count="exact").eq("code", code).execute()
    line(MEH, f"link opens so far: {clicks.count or 0}")

    refs = db.table("referrals").select("id").eq("affiliate_id", aff["id"]).execute().data or []
    line(MEH, f"referrals so far: {len(refs)}")

    print(
        "\n  What to do, in this order:\n"
        f"    1. Open {FRONTEND}/?ref={code} in a CLEAN profile (no cookies, not your\n"
        "       own account — self-referral is rejected by the database on purpose).\n"
        "    2. Sign up with a fresh email. Attribution is FIRST TOUCH: visiting the\n"
        "       plain site first and the ref link second credits nobody.\n"
        "    3. Subscribe with a real card (a $12 week is the cheapest honest test).\n"
        f"    4. Run: .venv/bin/python scripts/affiliate_live_test.py verify {code}\n"
        f"    5. Refund that payment in Stripe and run verify again — the commission\n"
        "       must flip to 'reversed'. An accrual that cannot be clawed back is a\n"
        "       worse bug than one that never accrued.\n"
    )


def cmd_verify(args) -> None:
    code = args.code.strip().lower()
    db = get_supabase()
    print(f"\nVerifying the chain for ?ref={code}\n")

    aff = affiliate_row(code)
    if not aff:
        line(BAD, f"No affiliate with code '{code}'.")
        sys.exit(1)
    line(OK, f"affiliate {code} ({aff['commission_pct']}%, {aff['status']})")

    # 1. attribution — the browser's ?ref= survived signup
    profiles = (
        db.table("profiles")
        .select("user_id, attribution, attributed_at")
        .eq("attribution->>ref", code)
        .execute()
        .data
        or []
    )
    if not profiles:
        line(BAD, "no profile carries this ref — the link never reached signup")
        print("        Cause is client-side: first-touch cookie missing, a different\n"
              "        browser profile, or they signed up before opening the link.")
        sys.exit(1)
    line(OK, f"{len(profiles)} profile(s) attributed to this code")

    # 2. referral — the database trigger turned attribution into a referral
    referrals = (
        db.table("referrals")
        .select("id, referred_user_id, status, first_seen_at, first_paid_at")
        .eq("affiliate_id", aff["id"])
        .execute()
        .data
        or []
    )
    if not referrals:
        line(BAD, "attributed, but NO referral row — the trigger did not fire")
        print("        Check link_referral_from_attribution in migrations/add_affiliates.sql;\n"
              "        it only matches affiliates with status='active'.")
        sys.exit(1)
    paying = [r for r in referrals if r.get("first_paid_at")]
    line(OK, f"{len(referrals)} referral(s), {len(paying)} marked as paying")

    # 3. commission — Stripe's invoice.paid became money owed
    commissions = (
        db.table("commissions")
        .select("id, stripe_invoice_id, gross_cents, amount_cents, commission_pct, status, created_at")
        .eq("affiliate_id", aff["id"])
        .order("created_at", desc=True)
        .execute()
        .data
        or []
    )
    if not commissions:
        line(BAD, "referral exists but NO commission — no invoice.paid arrived, or it did not accrue")
        print("        Check, in this order:\n"
              "          * did the payment actually succeed in Stripe?\n"
              "          * is the webhook endpoint subscribed to invoice.paid (not just\n"
              "            checkout.session.completed)?\n"
              "          * did the Railway logs show '[affiliate]' on that delivery?\n")
        sys.exit(1)

    for c in commissions:
        expected = round(c["gross_cents"] * float(c["commission_pct"]) / 100)
        correct = expected == c["amount_cents"]
        line(
            OK if correct else BAD,
            f"{c['created_at'][:10]} {c['stripe_invoice_id']}: paid {money(c['gross_cents'])} "
            f"→ {money(c['amount_cents'])} at {c['commission_pct']}% [{c['status']}]"
            + ("" if correct else f"  EXPECTED {money(expected)}"),
        )

    reversed_ = [c for c in commissions if c["status"] == "reversed"]
    print(
        f"\n  Accrued: {money(sum(c['amount_cents'] for c in commissions if c['status'] == 'accrued'))}"
        f" · reversed: {money(sum(c['amount_cents'] for c in reversed_))}"
        f" · paid out: {money(sum(c['amount_cents'] for c in commissions if c['status'] == 'paid_out'))}"
    )
    print(
        "\n  Still to prove after this: refund the payment in Stripe and run verify\n"
        "  again — the row must read 'reversed'.\n"
        if not reversed_
        else "\n  Clawback proven: a refund reversed the commission.\n"
    )


def cmd_clicks(args) -> None:
    """Did the counter actually record the opens? Also the print A/B: card vs stickers."""
    db = get_supabase()
    now = datetime.now(UTC).isoformat()
    rows = db.rpc("affiliate_click_totals", {"p_from": "1970-01-01T00:00:00Z", "p_to": now}).execute().data or []
    if not rows:
        print("\n  No link opens recorded yet.\n")
        return
    width = max(len(r["code"]) for r in rows)
    print(f"\n  {'code'.ljust(width)}  opens")
    for r in sorted(rows, key=lambda r: -r["clicks_total"]):
        print(f"  {r['code'].ljust(width)}  {r['clicks_total']}")
    print()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("preflight", help="is this code ready for a live payment test")
    p.add_argument("code")
    p.set_defaults(func=cmd_preflight)

    p = sub.add_parser("verify", help="walk attribution → referral → commission")
    p.add_argument("code")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("clicks", help="opens per code, partner and printed alike")
    p.set_defaults(func=cmd_clicks)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
