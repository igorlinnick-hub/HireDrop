#!/usr/bin/env python3
"""Affiliate program admin — issue codes, read the ledger, record payouts.

There is deliberately no self-serve signup: codes are handed out one by one
after a conversation (Igor's call, 2026-09-15). At five to ten partners that is
cheaper than building a signup funnel, and it keeps the program free of the
farmed accounts that make affiliate schemes worthless.

Everything below writes with service_role, so it bypasses RLS by design — the
affiliate's own dashboard reads the same rows through the policies in
migrations/add_affiliates.sql.

USAGE
  .venv/bin/python scripts/affiliate_admin.py list
  .venv/bin/python scripts/affiliate_admin.py issue lauren@uni.edu --code lauren
  .venv/bin/python scripts/affiliate_admin.py issue x@y.com --code cole --pct 35 \\
      --paypal cole@paypal.com
  .venv/bin/python scripts/affiliate_admin.py ledger lauren
  .venv/bin/python scripts/affiliate_admin.py payout lauren --ref 8FH12345 --yes
  .venv/bin/python scripts/affiliate_admin.py status lauren banned

`payout` only settles commissions older than the 30-day Stripe refund window and
never below the $25 minimum we promise — both are checks, not suggestions: it
refuses rather than paying out money that can still be clawed back.
"""

import argparse
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402,F401 — importing it loads .env (SUPABASE_* below)
from app.db.client import get_supabase  # noqa: E402

CODE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,38}$")
REFUND_WINDOW_DAYS = 30
MIN_PAYOUT_CENTS = 2500  # $25 — the minimum quoted in the affiliate kit


def money(cents: int) -> str:
    return f"${cents / 100:,.2f}"


def find_user_by_email(email: str) -> str | None:
    """auth.users has no PostgREST surface; go through the admin API."""
    email = email.strip().lower()
    page = 1
    while page <= 20:  # 20k users is far past where this script stops being the tool
        users = get_supabase().auth.admin.list_users(page=page, per_page=1000)
        if not users:
            return None
        for u in users:
            if (getattr(u, "email", "") or "").lower() == email:
                return str(u.id)
        if len(users) < 1000:
            return None
        page += 1
    return None


def get_affiliate(code: str) -> dict:
    res = get_supabase().table("affiliates").select("*").eq("code", code.lower()).execute()
    if not res.data:
        sys.exit(f"No affiliate with code '{code}'. `list` shows them all.")
    return res.data[0]


def cmd_issue(args) -> None:
    code = args.code.lower()
    if not CODE_RE.match(code):
        sys.exit(f"Bad code '{code}': lowercase letters/digits/._- , 2-39 chars.")

    user_id = find_user_by_email(args.email)
    if not user_id:
        sys.exit(f"No HireDrop account for {args.email} — they sign up first, then get a code.")

    existing = get_supabase().table("affiliates").select("code").eq("user_id", user_id).execute()
    if existing.data:
        sys.exit(f"{args.email} already has code '{existing.data[0]['code']}'.")

    row = {
        "user_id": user_id,
        "code": code,
        "commission_pct": args.pct,
        "paypal_email": args.paypal,
        "note": args.note,
    }
    get_supabase().table("affiliates").insert(row).execute()

    # Anyone who already signed up under this code (print material, early
    # sharing) is linked now — the trigger only fires on new attribution writes.
    linked = backfill_referrals(code)

    print(f"Issued {code} to {args.email} at {args.pct}%.")
    print(f"  link:      https://hiredrop.io/?ref={code}")
    print("  dashboard: https://hiredrop.io/dashboard/affiliate")
    if linked:
        print(f"  backfilled {linked} existing signup(s) that already used ?ref={code}")
    if not args.paypal:
        print("  no PayPal email on file — add it before the first payout.")


def backfill_referrals(code: str) -> int:
    """Link profiles already carrying ?ref=<code> to this affiliate."""
    aff = get_affiliate(code)
    sb = get_supabase()
    profiles = sb.table("profiles").select("user_id, attribution").execute().data or []
    linked = 0
    for p in profiles:
        attribution = p.get("attribution") or {}
        if (attribution.get("ref") or "").lower() != code.lower():
            continue
        if p["user_id"] == aff["user_id"]:
            continue
        try:
            sb.table("referrals").upsert(
                {"affiliate_id": aff["id"], "referred_user_id": p["user_id"]},
                on_conflict="referred_user_id",
                ignore_duplicates=True,
            ).execute()
            linked += 1
        except Exception as e:
            print(f"  ! could not link {p['user_id']}: {e}", file=sys.stderr)
    return linked


def cmd_list(_args) -> None:
    sb = get_supabase()
    affiliates = sb.table("affiliates").select("*").order("created_at").execute().data or []
    if not affiliates:
        print("No affiliates yet. `issue <email> --code <code>` creates the first one.")
        return

    referrals = sb.table("referrals").select("affiliate_id, first_paid_at").execute().data or []
    commissions = (
        sb.table("commissions").select("affiliate_id, amount_cents, status").execute().data or []
    )

    print(
        f"{'code':<16}{'status':<10}{'%':>5}  {'signups':>7}{'paying':>7}"
        f"{'accrued':>11}{'paid out':>11}"
    )
    print("-" * 68)
    for a in affiliates:
        mine = [r for r in referrals if r["affiliate_id"] == a["id"]]
        cs = [c for c in commissions if c["affiliate_id"] == a["id"]]
        accrued = sum(c["amount_cents"] for c in cs if c["status"] == "accrued")
        paid = sum(c["amount_cents"] for c in cs if c["status"] == "paid_out")
        print(
            f"{a['code']:<16}{a['status']:<10}{float(a['commission_pct']):>5.0f}  "
            f"{len(mine):>7}{sum(1 for r in mine if r['first_paid_at']):>7}"
            f"{money(accrued):>11}{money(paid):>11}"
        )


def cmd_ledger(args) -> None:
    aff = get_affiliate(args.code)
    rows = (
        get_supabase()
        .table("commissions")
        .select("created_at, gross_cents, amount_cents, status, stripe_invoice_id")
        .eq("affiliate_id", aff["id"])
        .order("created_at", desc=True)
        .execute()
        .data
        or []
    )
    print(
        f"{aff['code']} — {float(aff['commission_pct']):.0f}%, status {aff['status']}, "
        f"paypal {aff['paypal_email'] or '(none on file)'}"
    )
    if not rows:
        print("No commissions yet.")
        return
    print(f"\n{'date':<12}{'customer paid':>14}{'commission':>12}{'status':>11}  invoice")
    for r in rows:
        print(
            f"{r['created_at'][:10]:<12}{money(r['gross_cents']):>14}"
            f"{money(r['amount_cents']):>12}{r['status']:>11}  {r['stripe_invoice_id']}"
        )
    accrued = sum(r["amount_cents"] for r in rows if r["status"] == "accrued")
    print(f"\nOwed now (incl. commissions still inside the refund window): {money(accrued)}")


def cmd_payout(args) -> None:
    aff = get_affiliate(args.code)
    cutoff = (datetime.now(UTC) - timedelta(days=REFUND_WINDOW_DAYS)).isoformat()
    sb = get_supabase()
    due = (
        sb.table("commissions")
        .select("id, amount_cents, created_at")
        .eq("affiliate_id", aff["id"])
        .eq("status", "accrued")
        .lt("created_at", cutoff)
        .execute()
        .data
        or []
    )
    total = sum(c["amount_cents"] for c in due)

    if not due:
        sys.exit(
            f"Nothing settled yet — commissions become payable {REFUND_WINDOW_DAYS} days "
            "after they accrue (Stripe's refund window)."
        )
    if total < MIN_PAYOUT_CENTS and not args.force:
        sys.exit(
            f"{money(total)} is under the {money(MIN_PAYOUT_CENTS)} minimum we quote. "
            "It rolls to next month, or pass --force."
        )
    if not aff["paypal_email"] and not args.ref:
        sys.exit("No PayPal email on file and no --ref given — record how you actually paid.")

    print(f"{aff['code']}: {len(due)} commission(s), {money(total)} -> {aff['paypal_email']}")
    if not args.yes:
        sys.exit("Send the money first, then re-run with --yes to record it.")

    payout = (
        sb.table("payouts")
        .insert(
            {
                "affiliate_id": aff["id"],
                "amount_cents": total,
                "method": args.method,
                "external_ref": args.ref,
                "note": args.note,
            }
        )
        .execute()
        .data[0]
    )
    for c in due:
        sb.table("commissions").update({"status": "paid_out", "payout_id": payout["id"]}).eq(
            "id", c["id"]
        ).execute()
    print(f"Recorded payout {payout['id']} — {len(due)} commission(s) marked paid_out.")


def cmd_status(args) -> None:
    aff = get_affiliate(args.code)
    get_supabase().table("affiliates").update({"status": args.status}).eq("id", aff["id"]).execute()
    print(f"{aff['code']}: {aff['status']} -> {args.status}")
    if args.status != "active":
        print("New signups under this code stop being linked; existing referrals stay.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("issue", help="create an affiliate code for an existing account")
    p.add_argument("email")
    p.add_argument("--code", required=True, help="the ?ref= value, lowercase")
    p.add_argument("--pct", type=float, default=30.0)
    p.add_argument("--paypal")
    p.add_argument("--note")
    p.set_defaults(func=cmd_issue)

    p = sub.add_parser("list", help="every affiliate with signups, payers and money")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("ledger", help="one affiliate's commissions")
    p.add_argument("code")
    p.set_defaults(func=cmd_ledger)

    p = sub.add_parser("payout", help="record money you already sent")
    p.add_argument("code")
    p.add_argument("--ref", help="PayPal transaction id")
    p.add_argument("--method", default="paypal")
    p.add_argument("--note")
    p.add_argument("--force", action="store_true", help="pay below the $25 minimum")
    p.add_argument("--yes", action="store_true", help="confirm the money has been sent")
    p.set_defaults(func=cmd_payout)

    p = sub.add_parser("status", help="pending | active | banned")
    p.add_argument("code")
    p.add_argument("status", choices=["pending", "active", "banned"])
    p.set_defaults(func=cmd_status)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
