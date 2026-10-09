#!/usr/bin/env python3
"""Erase one HireDrop account — the tool behind the privacy policy's deletion promise.

jobflow-website app/privacy/page.tsx promises: email support@hiredrop.io from the account
address and "we will erase your profile, resume, application history, and stored sessions
within 30 days, except where retention is required by law (for example, billing records)".
This script is that promise. Run it on such an email; paste the receipt into the reply log.

    .venv/bin/python scripts/delete_account.py --email someone@x.com          # dry run
    .venv/bin/python scripts/delete_account.py --user-id <uuid>                 # dry run
    .venv/bin/python scripts/delete_account.py --email someone@x.com \\
        --execute --confirm <user id printed by the dry run>                    # erase

DRY RUN BY DEFAULT: prints, per table, how many rows would go, every file under
resumes/<uid>/, and the auth user — and deletes nothing. --confirm takes the user id the
dry run printed, not the email: a typo repeated in --email and --confirm would otherwise
resolve to, and erase, a different real account.

--execute refuses (exit 2, nothing deleted) when:
  * Stripe still bills them (active/trialing/past_due/unpaid/incomplete/paused) or Stripe
    cannot be asked — cancel first, a deleted account with a live subscription keeps
    charging a card. Stripe is asked by profile ids AND by email: checkout without a
    stored customer passes only customer_email (app/routers/billing.py), so a missed
    webhook leaves a paying customer that the profile does not name;
  * they are an affiliate with commissions or payouts rows — money records kept for legal
    retention, and affiliates.user_id cascades from auth.users, so deleting the auth user
    would wipe that ledger;
  * they were REFERRED and an affiliate earned commission on them, while
    referrals.referred_user_id is still NOT NULL (apply
    migrations/referrals_survive_account_erasure.sql — then the referral is anonymized
    instead of cascading another person's commissions away).

Before anything else it reads the live PostgREST schema and exits 3 if any table has a
column like *user_id / *email / *affiliate_id that RULES below does not classify — a
new table must never be skipped silently — or if a table RULES needs is missing from it
(an empty or unreadable schema would otherwise pass every check, and the referral
refusal with it). Then: ban the auth user (no new writes while we erase), storage files,
rows (counted and re-checked to zero), auth.admin.delete_user, and a last sweep for
anything written in the window before the ban took hold.

Exit codes: 0 ok · 1 usage / not found · 2 refused · 3 schema guard · 4 partial (re-run).
"""

import argparse
import math
import os
import re
import sys
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

import config  # noqa: E402 — importing it loads .env (SUPABASE_*, STRIPE_SECRET_KEY)
from app.db.client import PAGE, get_supabase  # noqa: E402

BUCKET = "resumes"
FILE_PAGE = 100  # storage list() default page; a folder holds 80+ job_<id>_tailored.pdf
# app/routers/billing.py's set plus the two that can still charge later: `incomplete`
# (first payment pending, payable for 23h) and `paused` (resumes billing when unpaused).
LIVE_SUB_STATUSES = ("active", "trialing", "past_due", "unpaid", "incomplete", "paused")
BAN_DURATION = "876000h"  # ~100 years; the account is deleted minutes later anyway

# Columns that point at a person. Any match in the live schema must be in RULES.
USER_COLUMN = re.compile(
    r"user_id$|email|affiliate_id$|owner_id$|referrer_id$|profile_id$|customer_id$|account_id$"
)

# USER_COLUMN matches that are not a HireDrop person.
NOT_A_PERSON = {
    ("ad_spend", "account_id"): "Meta/Google ad account id — ours, not a user's",
}

# RULES tables the API may legitimately not expose. Every other RULES table must be in
# the live schema, or the guard fails closed.
MAY_BE_ABSENT = {"extension_status"}
# Columns the plan reads besides the RULES ones.
EXTRA_COLUMNS = (("commissions", "referral_id"),)

# What each key resolves to: "uid" = the auth user id (uuid or its text form), "email" =
# the account address (lowercased), "affiliate" = ids of the affiliates row(s) they own.
ERASE, REFERRED, MONEY, COVERED = "erase", "referred", "money", "covered"


@dataclass(frozen=True)
class Rule:
    table: str
    column: str
    key: str  # uid | email | affiliate | - (covered)
    action: str
    reason: str


# Order = deletion order: children before parents, profiles last, auth user after all.
RULES: tuple[Rule, ...] = (
    Rule("interview_kits", "user_id", "uid", ERASE, "kit built from their applications"),
    Rule("tap_reviews", "user_id", "uid", ERASE, "their pending tap card"),
    Rule("handbacks", "user_id", "uid", ERASE, "forms handed back to them, with answers"),
    Rule(
        "screener_answer_cache",
        "user_id",
        "uid",
        ERASE,
        "their screener answers (visa, salary — PII)",
    ),
    Rule("applications", "user_id", "uid", ERASE, "application history (named in policy)"),
    Rule("jobs", "user_id", "uid", ERASE, "their deck incl. tailored resume text"),
    Rule("campaign_screenshots", "user_id", "uid", ERASE, "screenshots of forms they filled"),
    Rule("campaign_states", "user_id", "uid", ERASE, "their campaign filters/state"),
    Rule("activity_log", "user_id", "uid", ERASE, "their run log"),
    Rule("cover_letter_usage", "user_id", "uid", ERASE, "usage counters keyed to them"),
    Rule("user_timezones", "user_id", "uid", ERASE, "their time zone"),
    Rule(
        "ai_calls",
        "user_id",
        "uid",
        ERASE,
        "AI spend charged to them; past periods' totals drop by their share",
    ),
    Rule("ai_calls_daily", "user_id", "-", COVERED, "view over ai_calls; goes with its rows"),
    Rule("keyword_yield", "user_id", "uid", ERASE, "how their search phrases performed"),
    Rule(
        "extension_status", "user_id", "uid", ERASE, "extension heartbeat (skipped if not in API)"
    ),
    Rule("extension_keys", "user_id", "uid", ERASE, "stored sessions (named in policy)"),
    Rule("extension_keys", "email", "email", ERASE, "same keys, matched by cached email"),
    Rule(
        "promo_redemptions",
        "user_id",
        "uid",
        ERASE,
        "promo audit; a promo is free, not a billing record",
    ),
    Rule(
        "conversations",
        "user_id",
        "uid",
        ERASE,
        "legacy IG/TG bot; ids are social ids, erase if one ever equals uid",
    ),
    Rule("outbound_log", "user_id", "uid", ERASE, "legacy bot log, same as conversations"),
    Rule("waitlist", "email", "email", ERASE, "pre-signup waitlist entry"),
    Rule(
        "referrals",
        "referred_user_id",
        "uid",
        REFERRED,
        "their signup attribution; ANONYMIZED when an affiliate earned commission on it "
        "(commissions.referral_id cascades — erasing it would delete another person's money)",
    ),
    Rule("commissions", "affiliate_id", "affiliate", MONEY, "affiliate money ledger — refuse"),
    Rule("payouts", "affiliate_id", "affiliate", MONEY, "money paid out — refuse"),
    Rule("affiliate_applications", "email", "email", ERASE, "their affiliate application"),
    Rule("affiliate_applications", "affiliate_id", "affiliate", ERASE, "same, linked by id"),
    Rule("affiliate_applications", "paypal_email", "-", COVERED, "goes with the row"),
    Rule(
        "referrals",
        "affiliate_id",
        "affiliate",
        ERASE,
        "their signup list as affiliate; $0 (MONEY refusal guarantees no commissions)",
    ),
    Rule("affiliates", "user_id", "uid", ERASE, "their affiliate row (no money, see MONEY)"),
    Rule("affiliates", "paypal_email", "-", COVERED, "goes with the row"),
    Rule(
        "affiliates",
        "stripe_account_id",
        "-",
        COVERED,
        "goes with the row; the Connect account itself stays in Stripe (receipt names it)",
    ),
    Rule(
        "profiles",
        "user_id",
        "uid",
        ERASE,
        "profile + resume links (named in policy); invoices stay in Stripe",
    ),
    Rule("profiles", "stripe_customer_id", "-", COVERED, "goes with the row"),
)

# Tables with no column pointing at a HireDrop person — listed so a reader sees they were
# looked at, not forgotten. The guard needs no entry for them (no USER_COLUMN match).
NOT_USER_KEYED = {
    "ad_spend": "ad-platform spend, no person",
    "affiliate_clicks": "salted visitor hash per referral code — pseudonymous by design",
    "bot_settings": "config",
    "corrections": "legacy bot prompt corrections, no person key",
    "invite_codes": "codes",
    "platform_selectors": "config",
    "promo_codes": "codes; the `uses` counter is not personal",
    "stripe_events": "event-id dedup, no person; billing lives in Stripe",
    "training_examples": "legacy IG bot transcripts; no HireDrop user key (user_message is "
    "the message text, not an id) — third-party PII, separate cleanup, not an account",
}


class RefusedError(Exception):
    pass


class SchemaGuardError(Exception):
    pass


class PartialError(Exception):
    pass


@dataclass
class Plan:
    user_id: str
    email: str | None
    auth_exists: bool
    profile: dict
    affiliate_ids: list[str]
    connect_accounts: list[str] = field(default_factory=list)
    stripe_customers: list[str] = field(default_factory=list)
    counts: list[tuple[Rule, int | None]] = field(default_factory=list)
    referral_erase: list[str] = field(default_factory=list)
    referral_anonymize: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    refusals: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- schema
def fetch_schema() -> dict:
    """PostgREST OpenAPI `definitions`: {table: {properties: {...}, required: [...]}}."""
    url = os.environ["SUPABASE_URL"].rstrip("/") + "/rest/v1/"
    key = os.environ["SUPABASE_SERVICE_KEY"]
    resp = httpx.get(
        url,
        headers={
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Accept": "application/openapi+json",
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json().get("definitions") or {}


def unclassified_columns(schema: dict) -> list[str]:
    known = {(r.table, r.column) for r in RULES} | set(NOT_A_PERSON)
    out = []
    for table, d in sorted(schema.items()):
        for col in (d or {}).get("properties") or {}:
            if USER_COLUMN.search(col) and (table, col) not in known:
                out.append(f"{table}.{col}")
    return out


def missing_from_schema(schema: dict) -> list[str]:
    """What the plan needs but the live schema does not show. Non-empty = we are not
    looking at the real schema (OpenAPI off, a v3 shape, an empty reply), and every
    `table in schema` check below would quietly turn into "skip"."""
    out = []
    needed = [(r.table, r.column) for r in RULES if r.action != COVERED]
    for table, column in needed + list(EXTRA_COLUMNS):
        if table in MAY_BE_ABSENT and table not in schema:
            continue
        props = (schema.get(table) or {}).get("properties") or {}
        if column not in props:
            out.append(f"{table}.{column}")
    # Nullability of referred_user_id decides anonymize-vs-refuse; it is read from
    # `required`, so a definition without that list must not read as "nullable".
    if "id" not in ((schema.get("referrals") or {}).get("required") or []):
        out.append("referrals.required (NOT NULL list)")
    return sorted(set(out))


def check_schema(schema: dict) -> None:
    absent = missing_from_schema(schema)
    if absent:
        raise SchemaGuardError(
            "The live schema does not show what the plan needs — refusing rather than "
            "treating a missing table as empty:\n  " + "\n  ".join(absent)
        )
    missing = unclassified_columns(schema)
    if missing:
        raise SchemaGuardError(
            "These columns point at a person but scripts/delete_account.py RULES does not "
            "classify them — add a Rule (erase/keep/anonymize + reason) before erasing "
            "anyone:\n  " + "\n  ".join(missing)
        )


# --------------------------------------------------------------------------- reads
def find_user(sb, email: str | None, user_id: str | None) -> tuple[str, str | None, bool]:
    """(user_id, email, auth user exists)."""
    if user_id:
        uid = str(uuid.UUID(user_id))
        try:
            res = sb.auth.admin.get_user_by_id(uid)
        except Exception as e:
            # Only an explicit not-found means "no auth user". Anything else (network,
            # 5xx, bad key) must stop us: treating it as absent would erase the rows,
            # skip delete_user, and print a receipt saying the account is gone.
            if getattr(e, "status", None) == 404 or getattr(e, "code", None) == "user_not_found":
                return uid, (email or None), False
            raise LookupError(f"Could not read auth user {uid}: {e}") from e
        user = getattr(res, "user", None)
        if user is None:
            raise LookupError(f"Auth API answered for {uid} without a user; not guessing.")
        return uid, (getattr(user, "email", None) or "").lower() or None, True

    wanted = (email or "").strip().lower()
    for page in range(1, 51):  # 50k users — far past where this script stops being the tool
        users = sb.auth.admin.list_users(page=page, per_page=1000) or []
        for u in users:
            if (getattr(u, "email", "") or "").lower() == wanted:
                return str(uuid.UUID(str(u.id))), wanted, True
        if len(users) < 1000:
            break
    raise LookupError(f"No auth user with email {wanted}. Try --user-id if you have it.")


def count_rows(sb, table: str, column: str, values: list[str]) -> int:
    """Exact count — Content-Range is COUNT(*) on the server, not capped at max-rows."""
    if not values:
        return 0
    res = sb.table(table).select(column, count="exact").in_(column, values).limit(1).execute()
    return int(res.count or 0)


def select_paged(sb, table: str, columns: str, column: str, values: list[str]) -> list[dict]:
    """Every matching row, PAGE at a time (PostgREST silently stops at 1000)."""
    if not values:
        return []
    out: list[dict] = []
    start = 0
    while True:
        rows = (
            sb.table(table)
            .select(columns)
            .in_(column, values)
            .order("id")
            .range(start, start + PAGE - 1)
            .execute()
            .data
            or []
        )
        out.extend(rows)
        if len(rows) < PAGE:
            return out
        start += PAGE


def list_files(sb, user_id: str) -> list[str]:
    """Every object under resumes/<uid>/, recursing into subfolders, FILE_PAGE at a time."""
    folder = str(uuid.UUID(user_id))  # never "" — an empty prefix is EVERYONE's files
    bucket = sb.storage.from_(BUCKET)
    out: list[str] = []
    stack = [folder]
    while stack:
        path = stack.pop()
        offset = 0
        while True:
            items = (
                bucket.list(
                    path=path,
                    options={
                        "limit": FILE_PAGE,
                        "offset": offset,
                        "sortBy": {"column": "name", "order": "asc"},
                    },
                )
                or []
            )
            for it in items:
                name = it.get("name")
                if not name:
                    continue
                if "/" in name or name in (".", ".."):  # a name must not climb out
                    raise RuntimeError(f"storage listing returned odd name {name!r} in {path}")
                if it.get("id") is None:  # a folder, not an object
                    stack.append(f"{path}/{name}")
                else:
                    out.append(f"{path}/{name}")
            if len(items) < FILE_PAGE:
                break
            offset += FILE_PAGE
    return sorted(set(out))


def stripe_live_subscriptions(
    customer_id: str | None, subscription_id: str | None, email: str | None
):
    """(live subscriptions as ['sub_x (active)'], every customer id seen), or None when
    Stripe can't be asked. Customers are found by the profile id AND by email."""
    key = getattr(config, "STRIPE_SECRET_KEY", "")
    if not key:
        return None
    try:
        import stripe

        stripe.api_key = key
        customers = {customer_id} if customer_id else set()
        if email:  # Stripe's email filter is exact-case; auth emails are lowercase
            for c in stripe.Customer.list(email=email, limit=100).auto_paging_iter():
                customers.add(c["id"])
        found: dict[str, str] = {}
        for cus in sorted(customers):
            subs = stripe.Subscription.list(customer=cus, status="all", limit=100)
            for s in subs.auto_paging_iter():
                found[s["id"]] = s["status"]
        if subscription_id and subscription_id not in found:
            s = stripe.Subscription.retrieve(subscription_id)
            found[s["id"]] = s["status"]
            c = s.get("customer")
            if c:
                customers.add(c if isinstance(c, str) else c["id"])
        live = [f"{i} ({st})" for i, st in found.items() if st in LIVE_SUB_STATUSES]
        return live, sorted(customers)
    except Exception as e:
        print(f"  ! Stripe check failed: {e}", file=sys.stderr)
        return None


# --------------------------------------------------------------------------- plan
def build_plan(sb, schema: dict, user_id: str, email: str | None, auth_exists: bool, stripe_check):
    prof_rows = (
        sb.table("profiles")
        .select(
            "user_id, stripe_customer_id, stripe_subscription_id, subscription_tier, "
            "subscription_expires_at"
        )
        .eq("user_id", user_id)
        .execute()
        .data
        or []
    )
    profile = prof_rows[0] if prof_rows else {}
    aff = (
        sb.table("affiliates")
        .select("id, code, stripe_account_id")
        .eq("user_id", user_id)
        .execute()
        .data
        or []
    )
    plan = Plan(user_id, email, auth_exists, profile, [a["id"] for a in aff])
    plan.connect_accounts = [a["stripe_account_id"] for a in aff if a.get("stripe_account_id")]

    values = {
        "uid": [user_id],
        "email": [email] if email else [],
        "affiliate": plan.affiliate_ids,
    }
    for rule in RULES:
        if rule.action == COVERED:
            continue
        if rule.table not in schema:
            plan.counts.append((rule, None))  # not reachable through the API
            continue
        plan.counts.append((rule, count_rows(sb, rule.table, rule.column, values[rule.key])))

    # (a) Stripe still billing them — asked every time, not only when the profile names a
    # customer: checkout sends customer_email when there is none, and if the webhook that
    # writes stripe_customer_id back was missed, only the email finds the subscription.
    cus, sub = profile.get("stripe_customer_id"), profile.get("stripe_subscription_id")
    if not (cus or sub or email):
        plan.refusals.append(
            "No Stripe customer, subscription or email to look them up by — cannot rule out "
            "a live subscription. Re-run with --email, or find them in Stripe by hand."
        )
    else:
        res = stripe_check(cus, sub, email)
        if res is None:
            plan.refusals.append(
                f"Stripe could not be checked (customer {cus} / subscription {sub} / email "
                f"{email}). Verify in the Stripe dashboard and cancel any live subscription "
                "first."
            )
        else:
            live, plan.stripe_customers = res
            if live:
                plan.refusals.append(
                    f"Live Stripe subscription(s): {', '.join(live)} on customer(s) "
                    f"{', '.join(plan.stripe_customers)}. Cancel first (Stripe → Customers → "
                    "cancel immediately, or the user via the billing portal). Then re-run."
                )

    # (b) Affiliate money records.
    if plan.affiliate_ids:
        n_comm = count_rows(sb, "commissions", "affiliate_id", plan.affiliate_ids)
        n_pay = count_rows(sb, "payouts", "affiliate_id", plan.affiliate_ids)
        if n_comm or n_pay:
            codes = ", ".join(a.get("code") or a["id"] for a in aff)
            plan.refusals.append(
                f"Affiliate {codes} has {n_comm} commission(s) and {n_pay} payout(s) — money "
                "records kept for legal retention (tax/1099, disputes). affiliates.user_id "
                "cascades from auth.users, so deleting the account would wipe that ledger. "
                "Manually: settle or void accrued commissions (scripts/affiliate_admin.py "
                "ledger), set affiliates.status='banned', null paypal_email/note; erase the "
                "rest of their data by hand and keep the auth user disabled until a schema "
                "change lets the ledger outlive the account. Reply to the user saying "
                "payout records are retained as required by law."
            )

    # Referred side: whose commissions hang off their referral row?
    if "referrals" in schema:
        refs = select_paged(sb, "referrals", "id", "referred_user_id", [user_id])
        for r in refs:
            if count_rows(sb, "commissions", "referral_id", [r["id"]]):
                plan.referral_anonymize.append(r["id"])
            else:
                plan.referral_erase.append(r["id"])
        nullable = "referred_user_id" not in (schema["referrals"].get("required") or [])
        if plan.referral_anonymize and not nullable:
            plan.refusals.append(
                f"They were referred and an affiliate earned commission on them "
                f"(referral {', '.join(plan.referral_anonymize)}). commissions.referral_id "
                "cascades from referrals, and referrals.referred_user_id cascades from "
                "auth.users and is NOT NULL — deleting the account would delete another "
                "person's commissions. Apply migrations/referrals_survive_account_erasure.sql "
                "(lets the referral be anonymized), then re-run."
            )

    plan.files = list_files(sb, user_id)
    return plan


# --------------------------------------------------------------------------- execute
def _drain(sb, table: str, column: str, values: list[str], expected: int) -> None:
    """Delete until the count is zero — never trust one DELETE to take everything."""
    from postgrest.types import ReturnMethod

    for _ in range(math.ceil(expected / PAGE) + 3):
        sb.table(table).delete(returning=ReturnMethod.minimal).in_(column, values).execute()
        if count_rows(sb, table, column, values) == 0:
            return
    raise PartialError(f"{table}.{column}: rows still present after repeated deletes")


def _erase_files(sb, user_id: str, files: list[str]) -> int:
    bucket = sb.storage.from_(BUCKET)
    for i in range(0, len(files), FILE_PAGE):
        bucket.remove(files[i : i + FILE_PAGE])
    if list_files(sb, user_id):
        raise PartialError("storage: files remain under the folder after remove()")
    return len(files)


def execute(sb, plan: Plan) -> dict:
    """Ban → storage → rows → re-verify → auth user → sweep. Returns the receipt data."""
    if plan.refusals:
        raise RefusedError("\n".join(plan.refusals))
    done = {"files": 0, "rows": [], "anonymized": 0, "late": []}

    # Ban first: the site and the extension keep writing (applications, activity_log,
    # uploads) while a campaign runs, and rows written behind the drain would outlive the
    # account. A ban stops sign-in and token refresh; a token already issued lives out
    # its hour, which the sweep after delete_user covers.
    if plan.auth_exists:
        try:
            sb.auth.admin.update_user_by_id(plan.user_id, {"ban_duration": BAN_DURATION})
        except Exception as e:
            raise PartialError(f"could not ban the auth user, nothing deleted: {e}") from e
        done["banned"] = True

    done["files"] = _erase_files(sb, plan.user_id, plan.files)

    values = {
        "uid": [plan.user_id],
        "email": [plan.email] if plan.email else [],
        "affiliate": plan.affiliate_ids,
    }
    for rule, n in plan.counts:
        if not n:
            continue
        if rule.action == REFERRED:
            if plan.referral_anonymize:
                sb.table("referrals").update({"referred_user_id": None}).in_(
                    "id", plan.referral_anonymize
                ).eq("referred_user_id", plan.user_id).execute()
                done["anonymized"] = len(plan.referral_anonymize)
            if plan.referral_erase:
                _drain(sb, "referrals", "id", plan.referral_erase, len(plan.referral_erase))
            if count_rows(sb, "referrals", "referred_user_id", [plan.user_id]):
                raise PartialError("referrals: a row still points at the user")
            done["rows"].append((f"{rule.table}.{rule.column}", len(plan.referral_erase)))
            continue
        if rule.action != ERASE:
            continue  # MONEY with n>0 already refused
        # Re-count: an earlier rule may already have taken these rows (extension_keys by
        # user_id, then by email) — the receipt must not count a row twice.
        now = count_rows(sb, rule.table, rule.column, values[rule.key])
        if now:
            _drain(sb, rule.table, rule.column, values[rule.key], now)
            done["rows"].append((f"{rule.table}.{rule.column}", now))

    # Re-verify everything before the one step that cannot be re-run.
    for rule, n in plan.counts:
        if n is None or rule.action not in (ERASE, REFERRED):
            continue
        left = count_rows(sb, rule.table, rule.column, values[rule.key])
        if left:
            raise PartialError(
                f"{rule.table}.{rule.column}: {left} row(s) left, auth user NOT deleted"
            )

    if plan.auth_exists:
        try:
            sb.auth.admin.delete_user(plan.user_id)
        except Exception as e:
            raise PartialError(
                f"rows and files erased, but auth.admin.delete_user failed: {e}"
            ) from e
    done["auth"] = "deleted" if plan.auth_exists else "already absent"

    # Sweep: anything written between the plan and the ban taking hold.
    late_files = list_files(sb, plan.user_id)
    if late_files:
        done["files"] += _erase_files(sb, plan.user_id, late_files)
        done["late"].append(("storage", len(late_files)))
    for rule, n in plan.counts:
        if n is None or rule.action != ERASE:
            continue
        left = count_rows(sb, rule.table, rule.column, values[rule.key])
        if left:
            _drain(sb, rule.table, rule.column, values[rule.key], left)
            done["late"].append((f"{rule.table}.{rule.column}", left))
    done["at"] = datetime.now(UTC).isoformat(timespec="seconds")
    return done


# --------------------------------------------------------------------------- output
def print_plan(plan: Plan) -> None:
    who = plan.email or "(no email)"
    print(f"Account: {who}  user {plan.user_id}")
    print(f"  auth user: {'exists' if plan.auth_exists else 'NOT FOUND (rows only)'}")
    print(f"  confirm with: --execute --confirm {plan.user_id}")
    p = plan.profile
    if p:
        print(
            f"  billing: tier={p.get('subscription_tier')} "
            f"customer={p.get('stripe_customer_id')} sub={p.get('stripe_subscription_id')}"
        )
    if plan.stripe_customers:
        print(f"  Stripe customers (by id and email): {', '.join(plan.stripe_customers)}")
    for acct in plan.connect_accounts:
        print(f"  Stripe Connect account {acct}: stays in Stripe")
    print("  rows (table.column → rows, action):")
    for rule, n in plan.counts:
        shown = "not in API" if n is None else str(n)
        print(f"    {rule.table + '.' + rule.column:<38}{shown:>10}  {rule.action}")
    if plan.referral_anonymize:
        print(f"    referrals to ANONYMIZE (ledger kept): {len(plan.referral_anonymize)}")
    print(f"  storage {BUCKET}/{plan.user_id}/: {len(plan.files)} file(s)")
    for f in plan.files:
        print(f"    {f}")
    if plan.refusals:
        print("  --execute would REFUSE:")
        for r in plan.refusals:
            print(f"    - {r}")
    else:
        print("  --execute would proceed.")


def print_receipt(plan: Plan, done: dict) -> None:
    print("HireDrop account erasure — receipt")
    print(f"  erased_at:  {done['at']}")
    print(f"  account:    {plan.email or '(no email)'} (user {plan.user_id})")
    print(f"  storage:    {BUCKET}/{plan.user_id}/ — {done['files']} file(s) removed")
    print("  rows erased:")
    for name, n in done["rows"]:
        print(f"    {name:<38}{n:>8}")
    if not done["rows"]:
        print("    (none)")
    if done["anonymized"]:
        print(
            f"  anonymized: {done['anonymized']} referral row(s) — affiliate commission "
            "ledger kept, link to the person removed"
        )
    if done["late"]:
        late = ", ".join(f"{name} {n}" for name, n in done["late"])
        print(f"  late writes swept after the ban: {late}")
    for cus in plan.stripe_customers:
        print(
            f"  kept:       Stripe customer {cus} — invoices stay in Stripe (billing "
            "records, legal retention)"
        )
    for acct in plan.connect_accounts:
        print(
            f"  kept:       Stripe Connect account {acct} — owned by Stripe, not erased by "
            "us; the person closes it in their Stripe Express dashboard"
        )
    print(f"  auth user:  {done['auth']}")


def main(argv=None, sb=None, schema=None, stripe_check=stripe_live_subscriptions) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    who = ap.add_mutually_exclusive_group(required=True)
    who.add_argument("--email")
    who.add_argument("--user-id")
    ap.add_argument("--execute", action="store_true", help="actually erase (default: dry run)")
    ap.add_argument("--confirm", help="with --execute: the user id the dry run printed")
    args = ap.parse_args(argv)

    if args.execute and not args.confirm:
        print("--execute needs --confirm <user id from the dry run>.", file=sys.stderr)
        return 1

    sb = sb or get_supabase()
    try:
        uid, email, auth_exists = find_user(sb, args.email, args.user_id)
    except (LookupError, ValueError) as e:
        print(str(e), file=sys.stderr)
        return 1

    try:
        check_schema(schema if schema is not None else (schema := fetch_schema()))
    except SchemaGuardError as e:
        print(f"SCHEMA GUARD: {e}", file=sys.stderr)
        return 3

    if args.execute and args.confirm.strip().lower() != uid:
        print(
            f"--confirm '{args.confirm}' is not this account's user id ({uid} for "
            f"{email or 'no email'}). Run the dry run and copy its id. Nothing deleted.",
            file=sys.stderr,
        )
        return 1

    plan = build_plan(sb, schema, uid, email, auth_exists, stripe_check)
    if not args.execute:
        print("DRY RUN — nothing deleted.")
        print_plan(plan)
        return 0

    try:
        done = execute(sb, plan)
    except RefusedError as e:
        print(f"REFUSED — nothing deleted:\n{e}", file=sys.stderr)
        return 2
    except PartialError as e:
        print(f"PARTIAL — {e}\nRe-run the same command; it is idempotent.", file=sys.stderr)
        return 4
    print_receipt(plan, done)
    return 0


if __name__ == "__main__":
    sys.exit(main())
