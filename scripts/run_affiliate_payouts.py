#!/usr/bin/env python3
"""Daily affiliate payout run — Stripe Connect replaces the manual PayPal payout.

Pays every affiliate who is actually owed money and can actually receive it:
  - the commission is 30+ days old (REFUND_WINDOW_DAYS) and still `accrued`
  - the affiliate has a Connect account with payouts enabled (`account.updated`
    flipped `affiliates.payouts_enabled` — app/routers/billing.py)
  - the batch for that affiliate totals at least MIN_PAYOUT_CENTS ($25)

Moving money cannot pay twice, so idempotency is two-layered:
  1. a `pending` payout row CLAIMS its commissions (sets their payout_id)
     BEFORE any Stripe call. A second run sees them already claimed and will
     not bundle them into a new batch.
  2. the Transfer itself carries `idempotency_key=f"affiliate-payout-{row id}"`,
     so a crash between "claimed" and "Stripe confirmed" cannot create a
     second transfer on retry — Stripe hands back the SAME transfer it already
     made for that key.

Partners without a connected, payouts-enabled account are reported and left
alone; `scripts/affiliate_admin.py payout` still exists for a one-off manual
PayPal payout if that's ever needed again.

USAGE
  .venv/bin/python scripts/run_affiliate_payouts.py run [--dry-run] [--force]
"""

import argparse
import sys
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402,F401 — importing it loads .env (STRIPE_*, SUPABASE_*)
from app.db.client import get_supabase  # noqa: E402

REFUND_WINDOW_DAYS = 30  # mirrors scripts/affiliate_admin.py
MIN_PAYOUT_CENTS = 2500  # $25 — mirrors scripts/affiliate_admin.py


def money(cents: int) -> str:
    return f"${cents / 100:,.2f}"


def _stripe():
    if not config.STRIPE_SECRET_KEY:
        sys.exit("STRIPE_SECRET_KEY not set.")
    import stripe

    stripe.api_key = config.STRIPE_SECRET_KEY
    return stripe


def _settle(sb, stripe, payout: dict, account_id: str, dry_run: bool) -> bool:
    """Send one `pending` payout and mark it (and its claimed commissions) paid.

    True on success. False on a Stripe-side failure — the row is left `pending`
    for the next run to retry (same idempotency key, so retrying is safe).
    """
    if dry_run:
        print(f"  [dry-run] would transfer {money(payout['amount_cents'])} -> {account_id}")
        return True
    try:
        transfer = stripe.Transfer.create(
            amount=payout["amount_cents"],
            currency="usd",
            destination=account_id,
            description=f"HireDrop affiliate payout {payout['id']}",
            idempotency_key=f"affiliate-payout-{payout['id']}",
        )
    except Exception as exc:  # noqa: BLE001 — left pending, never raise out of a batch
        print(f"  ! transfer failed for payout {payout['id']}: {exc}", file=sys.stderr)
        return False

    sb.table("payouts").update(
        {
            "status": "completed",
            "stripe_transfer_id": transfer.id,
            "paid_at": datetime.now(UTC).isoformat(),
        }
    ).eq("id", payout["id"]).execute()
    sb.table("commissions").update({"status": "paid_out"}).eq("payout_id", payout["id"]).execute()
    print(f"  -> transfer {transfer.id}, {money(payout['amount_cents'])}")
    return True


def cmd_run(args) -> int:
    """Exit code: 0 when every attempted transfer went through, 1 otherwise.

    It runs as a Railway cron service, where the exit code is the only thing
    that turns a failed run red, and the closing summary line is the proof in
    the log that it ran at all. Before 10-06 a run with nothing owed printed
    nothing, and nothing scheduled it either, so a silent log meant nothing.
    """
    sb = get_supabase()
    stripe = None if args.dry_run else _stripe()
    paid = failed = 0
    paid_cents = 0

    affiliates = {
        a["id"]: a
        for a in (
            sb.table("affiliates")
            .select("id, user_id, code, status, stripe_account_id, payouts_enabled")
            .execute()
            .data
            or []
        )
    }

    # ---- 1. resume anything a previous run claimed but never confirmed with Stripe
    pending = (
        sb.table("payouts")
        .select("id, affiliate_id, amount_cents")
        .eq("status", "pending")
        .execute()
        .data
        or []
    )
    for payout in pending:
        aff = affiliates.get(payout["affiliate_id"])
        if not aff or not aff.get("stripe_account_id"):
            print(f"! pending payout {payout['id']} has no Connect account — settle by hand")
            continue

        # Reconcile against the commissions actually linked to this payout
        # before trusting its stored amount_cents. The insert (claiming the
        # row's own id) and the claim (setting commissions.payout_id) are two
        # separate writes — a crash between them leaves a `pending` payout
        # that claimed NOTHING. Paying its stored amount anyway would move
        # money for commissions that are still unclaimed and about to be
        # picked up fresh by step 2 below, in the SAME run — a double transfer.
        linked = (
            sb.table("commissions")
            .select("amount_cents")
            .eq("payout_id", payout["id"])
            .eq("status", "accrued")
            .execute()
            .data
            or []
        )
        actual = sum(r["amount_cents"] for r in linked)
        if actual != payout["amount_cents"]:
            print(
                f"  ! payout {payout['id']} claims {money(payout['amount_cents'])} but only "
                f"{money(actual)} of commissions are actually linked — reconciling"
            )
            if actual <= 0:
                if not args.dry_run:
                    sb.table("payouts").update({"status": "failed"}).eq(
                        "id", payout["id"]
                    ).execute()
                continue
            if not args.dry_run:
                sb.table("payouts").update({"amount_cents": actual}).eq(
                    "id", payout["id"]
                ).execute()
            payout["amount_cents"] = actual

        print(f"resuming payout {payout['id']} for {aff['code']} ({money(payout['amount_cents'])})")
        if _settle(sb, stripe, payout, aff["stripe_account_id"], args.dry_run):
            paid += 1
            paid_cents += payout["amount_cents"]
        else:
            failed += 1

    # ---- 2. find new batches among accrued commissions no payout has claimed yet
    cutoff = (datetime.now(UTC) - timedelta(days=REFUND_WINDOW_DAYS)).isoformat()
    due = (
        sb.table("commissions")
        .select("id, affiliate_id, amount_cents")
        .eq("status", "accrued")
        .is_("payout_id", "null")
        .lt("created_at", cutoff)
        .execute()
        .data
        or []
    )
    by_affiliate: dict[str, list[dict]] = defaultdict(list)
    for row in due:
        by_affiliate[row["affiliate_id"]].append(row)

    for affiliate_id, rows in by_affiliate.items():
        aff = affiliates.get(affiliate_id)
        if not aff or aff.get("status") != "active":
            continue
        total = sum(r["amount_cents"] for r in rows)
        if not aff.get("stripe_account_id"):
            print(f"{aff['code']}: {money(total)} accrued, no Connect account yet — skipped")
            continue
        if not aff.get("payouts_enabled"):
            print(f"{aff['code']}: {money(total)} accrued, Connect onboarding incomplete — skipped")
            continue
        if total < MIN_PAYOUT_CENTS and not args.force:
            print(
                f"{aff['code']}: {money(total)} under the {money(MIN_PAYOUT_CENTS)} minimum — rolls over"
            )
            continue

        ids = [r["id"] for r in rows]
        if args.dry_run:
            print(f"{aff['code']}: would pay {money(total)} ({len(ids)} commission(s))")
            continue

        payout = (
            sb.table("payouts")
            .insert(
                {
                    "affiliate_id": affiliate_id,
                    "amount_cents": total,
                    "method": "stripe_connect",
                    "status": "pending",
                }
            )
            .execute()
            .data[0]
        )
        # Claim ONLY commissions still unclaimed. This is a single daily cron,
        # not meant to run concurrently with itself — but if it ever did, a
        # race loses here (fewer rows claimed than planned), not by paying the
        # same commission twice.
        claimed = (
            sb.table("commissions")
            .update({"payout_id": payout["id"]})
            .in_("id", ids)
            .is_("payout_id", "null")
            .execute()
            .data
            or []
        )
        actual_total = sum(r["amount_cents"] for r in claimed)
        if len(claimed) != len(ids):
            print(
                f"  ! claimed {len(claimed)}/{len(ids)} commissions for {aff['code']} "
                "(raced with another run?) — re-pricing payout to what was actually claimed"
            )
        if actual_total != total:
            sb.table("payouts").update({"amount_cents": actual_total}).eq(
                "id", payout["id"]
            ).execute()
            payout["amount_cents"] = actual_total
        if actual_total <= 0:
            sb.table("payouts").update({"status": "failed"}).eq("id", payout["id"]).execute()
            continue

        print(f"{aff['code']}: paying {money(actual_total)} ({len(claimed)} commission(s))")
        if _settle(sb, stripe, payout, aff["stripe_account_id"], args.dry_run):
            paid += 1
            paid_cents += actual_total
        else:
            failed += 1

    print(
        f"payout run {'(dry-run) ' if args.dry_run else ''}done: {paid} paid ({money(paid_cents)}), "
        f"{failed} failed, {len(pending)} resumed, {len(due)} commission(s) past the "
        f"{REFUND_WINDOW_DAYS}-day window"
    )
    return 1 if failed else 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("run", help="pay everyone who is owed money and can receive it")
    p.add_argument("--dry-run", action="store_true", help="print what would happen, write nothing")
    p.add_argument("--force", action="store_true", help="pay below the $25 minimum")
    p.set_defaults(func=cmd_run)

    args = parser.parse_args()
    sys.exit(args.func(args) or 0)


if __name__ == "__main__":
    main()
