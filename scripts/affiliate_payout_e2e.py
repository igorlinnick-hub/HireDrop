#!/usr/bin/env python3
"""The affiliate payout run, end to end, on sandbox money against the real database.

The transfer itself was proven in the Stripe sandbox (10-06), and the ledger logic
is unit-tested with mocks. What neither covers is the real run against the real
schema: picking the due commission out of production tables, claiming it,
writing the payout row, and marking the commission paid. There is only one
database, so this test brings its own throwaway people and removes them after.

  .venv/bin/python scripts/affiliate_payout_e2e.py

What it does:
  1. Refuses to start if anything real is due or pending: the run it calls pays
     EVERY due commission in the table, not only ours.
  2. Creates two throwaway auth users (partner + buyer), an affiliate row wired
     to the sandbox Express account, a referral, and one $30 commission dated
     31 days ago.
  3. Calls the production payout run (scripts/run_affiliate_payouts.py) in
     process, with a TEST-mode Stripe key — a live key is refused, so no real
     money can move.
  4. Checks payouts/commissions and the Transfer in Stripe, prints PASS/FAIL.
  5. Deletes both users; the affiliate/referral/commission/payout rows go with
     them (on delete cascade). Cleanup runs even when a check fails.

Key: STRIPE_TEST_KEY in jobflow/.env — the sandbox secret key (Stripe → Sandbox →
Developers → API keys). The Stripe CLI keeps its key in the macOS keychain, not usable here.
"""

import argparse
import os
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402 — importing it loads .env
from app.db.client import get_supabase  # noqa: E402

SANDBOX_PARTNER = "acct_1ULvY5FMt3FtYjtC"  # test Express account, onboarded 10-06
AMOUNT_CENTS = 3000  # over the $25 minimum, so --force is not needed
OK, BAD = "PASS", "FAIL"


def test_key() -> str:
    key = os.getenv("STRIPE_TEST_KEY", "")
    if not key.startswith(("sk_test_", "rk_test_")):
        sys.exit(
            "STRIPE_TEST_KEY (sk_test_…) not in jobflow/.env. Refusing to run on anything else."
        )
    return key


def nothing_real_in_flight(sb) -> bool:
    cutoff = (datetime.now(UTC) - timedelta(days=30)).isoformat()
    due = (
        sb.table("commissions")
        .select("id")
        .eq("status", "accrued")
        .is_("payout_id", "null")
        .lt("created_at", cutoff)
        .execute()
        .data
    )
    pending = sb.table("payouts").select("id").eq("status", "pending").execute().data
    if due or pending:
        print(
            f"Real money in flight ({len(due)} due commission(s), {len(pending)} pending payout(s))."
        )
        print("This run would touch them too. Not starting.")
        return False
    return True


def main() -> int:
    argparse.ArgumentParser(description=__doc__.splitlines()[0]).parse_args()
    key = test_key()
    sb = get_supabase()
    if not nothing_real_in_flight(sb):
        return 2

    tag = uuid.uuid4().hex[:8]
    users: list[str] = []
    failures = 0

    def check(ok: bool, text: str) -> None:
        nonlocal failures
        failures += 0 if ok else 1
        print(f"  [{OK if ok else BAD}] {text}")

    try:
        for role in ("aff", "buyer"):
            u = sb.auth.admin.create_user(
                {
                    "email": f"e2e-payout-{role}-{tag}@hiredrop.io",
                    "password": uuid.uuid4().hex,
                    "email_confirm": True,
                    "user_metadata": {"e2e": "affiliate_payout_e2e"},
                }
            )
            users.append(u.user.id)
        aff_user, buyer_user = users

        aff = (
            sb.table("affiliates")
            .insert(
                {
                    "user_id": aff_user,
                    "code": f"e2e-{tag}",
                    "status": "active",
                    "stripe_account_id": SANDBOX_PARTNER,
                    "payouts_enabled": True,
                }
            )
            .execute()
            .data[0]
        )
        ref = (
            sb.table("referrals")
            .insert({"affiliate_id": aff["id"], "referred_user_id": buyer_user, "status": "paying"})
            .execute()
            .data[0]
        )
        com = (
            sb.table("commissions")
            .insert(
                {
                    "affiliate_id": aff["id"],
                    "referral_id": ref["id"],
                    "stripe_invoice_id": f"in_e2e_{tag}",
                    "gross_cents": 10000,
                    "amount_cents": AMOUNT_CENTS,
                    "commission_pct": 30,
                    "created_at": (datetime.now(UTC) - timedelta(days=31)).isoformat(),
                }
            )
            .execute()
            .data[0]
        )
        print(f"seeded: affiliate e2e-{tag} -> {SANDBOX_PARTNER}, commission $30.00 dated -31d")

        # The production run, unchanged, on a test key.
        config.STRIPE_SECRET_KEY = key
        import run_affiliate_payouts as run

        print("--- payout run ---")
        code = run.cmd_run(argparse.Namespace(dry_run=False, force=False))
        print("--- checks ---")
        check(code == 0, f"run exit code 0 (got {code})")

        c = (
            sb.table("commissions")
            .select("status, payout_id")
            .eq("id", com["id"])
            .execute()
            .data[0]
        )
        check(c["status"] == "paid_out", f"commission status paid_out (got {c['status']})")
        check(bool(c["payout_id"]), "commission linked to a payout")

        payouts = sb.table("payouts").select("*").eq("affiliate_id", aff["id"]).execute().data
        check(len(payouts) == 1, f"exactly one payout row (got {len(payouts)})")
        if payouts:
            p = payouts[0]
            check(p["status"] == "completed", f"payout status completed (got {p['status']})")
            check(
                p["amount_cents"] == AMOUNT_CENTS, f"payout amount $30.00 (got {p['amount_cents']})"
            )
            check(p["method"] == "stripe_connect", f"method stripe_connect (got {p['method']})")
            check(bool(p.get("paid_at")), "paid_at stamped")
            check(p["id"] == c["payout_id"], "commission points at this payout")
            tr_id = p.get("stripe_transfer_id")
            check(bool(tr_id), f"transfer id stored ({tr_id})")
            if tr_id:
                import stripe

                stripe.api_key = key
                tr = stripe.Transfer.retrieve(tr_id)
                check(tr.destination == SANDBOX_PARTNER, f"Stripe transfer -> {tr.destination}")
                check(tr.amount == AMOUNT_CENTS, f"Stripe transfer amount {tr.amount}c")

        print("--- second run must pay nothing ---")
        code2 = run.cmd_run(argparse.Namespace(dry_run=False, force=False))
        again = sb.table("payouts").select("id").eq("affiliate_id", aff["id"]).execute().data
        check(code2 == 0 and len(again) == 1, "re-run made no second payout")
    finally:
        for uid in users:
            try:
                sb.auth.admin.delete_user(uid)
            except Exception as e:  # noqa: BLE001 — report and keep cleaning
                print(f"  ! could not delete test user {uid}: {e} — delete by hand")
        left = sb.table("affiliates").select("id").eq("code", f"e2e-{tag}").execute().data
        print(f"cleanup: {len(users)} test user(s) deleted, affiliate rows left: {len(left)}")

    print(f"\n{'ALL PASS' if failures == 0 else f'{failures} FAILED'}")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    sys.exit(main())
