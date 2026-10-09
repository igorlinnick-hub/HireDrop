"""Admin metrics — the whole HireDrop admin dashboard as ONE read-only endpoint.

Why it lives here and not in a dashboard repo: the counting must sit next to the
data. The panel that renders this (Hellometrix, `/hiredrop`) is a thin screen —
it fetches this JSON and draws it. That split means the dashboard never holds a
service_role key for this database, and changing a metric is a change here, in
the product repo, not in someone else's app.

SECURITY
  * Read-only on product data. Every query below is a SELECT on user tables.
    The ONE write is the Ads section refreshing its own cache: when Meta spend
    is older than an hour it re-pulls the last 7 days of Insights into
    `ad_spend` (third-party numbers, never user data — app/ads/meta_spend.py).
  * Guarded by a shared secret (ADMIN_METRICS_TOKEN, header X-Admin-Token) and
    NOT by the user JWT dependency: the caller is a server-side dashboard, not a
    signed-in HireDrop user. Token unset -> 503, never "open".
  * Aggregates only. No resumes, cover letters, job URLs, emails, addresses or
    phone numbers leave this endpoint. The account roster carries first name,
    tier, source and counts, because that is what an owner's roster is.

COUNTED vs DERIVED — the distinction every cost number in this project got wrong
before it was measured: AI *calls* and *applications* are counted from tables;
*dollars* are applications x a constant measured on 2026-09-15 by
scripts/measure_ai_cost.py. `calls_per_application` is published next to it so a
stale constant announces itself instead of lying quietly.
"""

import hmac
import os
import re
import sys
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query

from app.ads import attribution as attr_rules
from app.ads import meta_spend
from app.ads import verdict as ad_rules
from app.billing_config import PLANS
from app.db import ad_spend as spend_db
from app.db import ai_calls as ai_calls_db
from app.db.client import fetch_paged, get_supabase
from config import FRONTEND_URL
from modules import ai_spend

router = APIRouter(prefix="/admin", tags=["admin"])

# --- constants ---------------------------------------------------------------

# Stripe's refund window: a commission younger than this can still be clawed
# back, so it is owed but not payable. Mirrors REFUND_WINDOW_DAYS in
# scripts/affiliate_admin.py — the two must agree or this dashboard promises
# money the payout tool refuses to send.
REFUND_WINDOW_DAYS = 30

# A campaign whose extension died keeps running=true forever; the heartbeat is
# what separates "running" from "actually alive".
HEARTBEAT_WINDOW_MIN = 5


# Row ceilings. At today's scale nothing comes close; a read that DID hit one
# would silently under-count, so each section says so in `notes`.
ROW_LIMIT = 20000
PROFILE_LIMIT = 5000
ROSTER_LIMIT = 50
SPENDER_LIMIT = 25
LOG_LIMIT = 5000

# Ads: the monthly test budget the "budget used" gauge is measured against.
DEFAULT_ADS_MONTHLY_BUDGET_USD = 500.0


# --- auth --------------------------------------------------------------------


def _require_admin(token: str | None) -> None:
    expected = os.getenv("ADMIN_METRICS_TOKEN", "")
    if not expected:
        # Fail closed: an unset secret must not mean "no check".
        raise HTTPException(status_code=503, detail="Admin metrics not configured")
    if not token or not hmac.compare_digest(token, expected):
        raise HTTPException(status_code=401, detail="Invalid admin token")


# --- small helpers -----------------------------------------------------------


def _metric(
    key: str,
    label: str,
    value: Any,
    fmt: str = "number",
    *,
    scope: str = "period",
    emphasis: bool = False,
    description: str | None = None,
) -> dict:
    """One number on the panel. `scope` tells the reader what window it covers:
    period | current | all_time — a mixed board without it is a lie by layout."""
    return {
        "key": key,
        "label": label,
        "value": value,
        "format": fmt,  # number | currency | percent | text
        "scope": scope,
        "emphasis": emphasis,
        "description": description,
    }


def _table(key: str, title: str, columns: list[dict], rows: list[dict]) -> dict:
    return {"key": key, "title": title, "columns": columns, "rows": rows}


def _col(key: str, label: str, fmt: str = "text", align: str = "left") -> dict:
    return {"key": key, "label": label, "format": fmt, "align": align}


def _rows(table: str, columns: str, limit: int = ROW_LIMIT, **filters) -> list[dict]:
    """SELECT with optional gte/lte/eq filters. Filter keys are 'col__op'."""
    q = get_supabase().table(table).select(columns)
    for spec, value in filters.items():
        col, _, op = spec.partition("__")
        if op == "gte":
            q = q.gte(col, value)
        elif op == "lte":
            q = q.lte(col, value)
        elif op == "eq":
            q = q.eq(col, value)
        elif op == "gt":
            q = q.gt(col, value)
    return q.limit(limit).execute().data or []


def _paged(table: str, columns: str, order: tuple[str, ...], limit: int = ROW_LIMIT) -> list[dict]:
    """SELECT an entire table past PostgREST's silent 1000-row cap.

    `_rows` asks for `limit` rows but PostgREST answers with at most 1000 and
    says nothing — for the all-time reads the board is built on (applications,
    profiles) that is a count that quietly plateaus. Pages are ordered by
    `order` then id so no row is duplicated or dropped between pages.
    """

    def build(start: int, end: int):
        q = get_supabase().table(table).select(columns)
        for col in order:
            q = q.order(col)
        return q.order("id").range(start, end)

    return fetch_paged(build, limit)


def _count(table: str, **filters) -> int | None:
    """Head-only count. Returns None on failure so one renamed column degrades
    a single metric instead of blanking the section."""
    try:
        q = get_supabase().table(table).select("id", count="exact")
        for spec, value in filters.items():
            col, _, op = spec.partition("__")
            if op == "gte":
                q = q.gte(col, value)
            elif op == "lte":
                q = q.lte(col, value)
            elif op == "eq":
                q = q.eq(col, value)
            elif op == "gt":
                q = q.gt(col, value)
            elif op == "not_null":
                q = q.not_.is_(col, "null")
        return q.limit(1).execute().count or 0
    except Exception:  # noqa: BLE001
        return None


def _daily(stamps: list[str]) -> list[dict]:
    buckets: dict[str, int] = defaultdict(int)
    for ts in stamps:
        if ts:
            buckets[ts[:10]] += 1
    return [{"date": d, "value": v} for d, v in sorted(buckets.items())]


def _tally(values: list[str]) -> list[tuple[str, int]]:
    counts: dict[str, int] = defaultdict(int)
    for v in values:
        counts[v] += 1
    return sorted(counts.items(), key=lambda kv: -kv[1])


def _pct(part: float | None, whole: float | None) -> float | None:
    if part is None or not whole:
        return None
    return round(part / whole * 1000) / 10


def _usd(cents: float) -> float:
    return round(cents) / 100


def _source_of(attribution: dict | None) -> str:
    # One definition, shared with the Ads section's channel classification.
    return attr_rules.source_of(attribution)


def _effective_tier(profile: dict, now_iso: str) -> str:
    """Tier as it should be READ, not as the column says: an expired paid tier
    is free. Mirrors get_tier() in the backend — a dashboard that disagreed with
    the product would be worse than none."""
    tier = profile.get("subscription_tier") or "free"
    if tier == "free":
        return "free"
    expires = profile.get("subscription_expires_at")
    return tier if expires and expires > now_iso else "free"


def _error_shape(message: str) -> str:
    """Collapse an error to its shape so the same failure counts once: ids, urls
    and numbers differ per occurrence and would splinter it into singletons."""
    text = re.sub(r"https?://\S+", "<url>", message or "")
    text = re.sub(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", "<id>", text, flags=re.I
    )
    return re.sub(r"\d+", "N", text)[:120]


# --- sections ----------------------------------------------------------------
# Each builder takes the shared reads and returns one panel section. A builder
# that raises is caught in the endpoint and rendered as an unavailable section:
# one broken query must not blank the board.


def _section_ops(apps: list[dict], from_ts: str, to_ts: str) -> dict:
    today_ts = f"{datetime.now(UTC).date().isoformat()}T00:00:00Z"
    heartbeat_cutoff = (datetime.now(UTC) - timedelta(minutes=HEARTBEAT_WINDOW_MIN)).isoformat()

    running = _count("campaign_states", running__eq=True)
    alive = _count("campaign_states", running__eq=True, last_ping_at__gte=heartbeat_cutoff) or 0

    period = [a for a in apps if from_ts <= (a.get("date_applied") or "") <= to_ts]
    today = [a for a in apps if (a.get("date_applied") or "") >= today_ts]

    metrics = [
        _metric(
            "applications_today",
            "Applications today",
            len(today),
            scope="current",
            emphasis=True,
            description="Submitted since midnight UTC — the 'is it working right now' number.",
        )
    ]
    if running is not None:
        stale = max(0, running - alive)
        metrics.append(
            _metric(
                "campaigns_alive",
                "Campaigns alive",
                alive,
                scope="current",
                emphasis=True,
                description=(
                    f"{stale} more read as running but haven't pinged in {HEARTBEAT_WINDOW_MIN} "
                    "minutes — extension closed or dead."
                    if stale
                    else "Running and pinging within the last 5 minutes."
                ),
            )
        )
        metrics.append(
            _metric(
                "campaigns_running",
                "Marked running",
                running,
                scope="current",
                description="What the database thinks is running, heartbeat ignored.",
            )
        )
    metrics.append(_metric("applications_period", "Applications sent", len(period)))

    errors = warnings = 0
    top_errors: list[dict] = []
    try:
        logs = (
            get_supabase()
            .table("activity_log")
            .select("level, phase, message, timestamp")
            .in_("level", ["error", "warn"])
            .gte("timestamp", from_ts)
            .lte("timestamp", to_ts)
            .order("timestamp", desc=True)
            .limit(LOG_LIMIT)
            .execute()
            .data
            or []
        )
        errors = sum(1 for r in logs if r.get("level") == "error")
        warnings = sum(1 for r in logs if r.get("level") == "warn")
        grouped: dict[str, dict] = {}
        for r in (r for r in logs if r.get("level") == "error"):
            shape = _error_shape(r.get("message") or "")
            entry = grouped.setdefault(shape, {"count": 0, "phase": r.get("phase") or "—"})
            entry["count"] += 1
        top_errors = [
            {"message": m, "count": v["count"], "phase": v["phase"]}
            for m, v in sorted(grouped.items(), key=lambda kv: -kv[1]["count"])[:10]
        ]
    except Exception as e:  # noqa: BLE001
        # activity_log unavailable — the rest of the section still renders, but a
        # silent pass here reads as "no errors today" when it means "we couldn't look".
        print(f"[admin] activity_log unavailable: {e}", file=sys.stderr)

    metrics.append(
        _metric("errors", "Errors", errors, description="activity_log rows at level=error.")
    )
    metrics.append(_metric("warnings", "Warnings", warnings))

    unconfirmed = sum(1 for a in period if a.get("status") == "applied_unconfirmed")
    if period:
        metrics.append(
            _metric(
                "unconfirmed_rate",
                "Unconfirmed",
                _pct(unconfirmed, len(period)),
                "percent",
                description="Share of submissions the extension could not confirm landed.",
            )
        )

    tables = [
        _table(
            "by_platform",
            "Applications by platform",
            [_col("platform", "Platform"), _col("count", "Sent", "number", "right")],
            [
                {"platform": p, "count": c}
                for p, c in _tally([a.get("platform") or "unknown" for a in period])
            ],
        ),
        _table(
            "by_status",
            "Applications by status",
            [_col("status", "Status"), _col("count", "Count", "number", "right")],
            [
                {"status": s, "count": c}
                for s, c in _tally([a.get("status") or "unknown" for a in period])
            ],
        ),
    ]
    if top_errors:
        tables.append(
            _table(
                "top_errors",
                "What broke most",
                [
                    _col("message", "Error"),
                    _col("phase", "Phase"),
                    _col("count", "Times", "number", "right"),
                ],
                top_errors,
            )
        )

    return {
        "key": "ops",
        "title": "Operations",
        "subtitle": "What is running right now, what got submitted, and what broke.",
        "metrics": metrics,
        "timeseries": {
            "label": "Applications per day",
            "format": "number",
            "points": _daily([a.get("date_applied") or "" for a in period]),
        },
        "tables": tables,
    }


def _section_users(profiles: list[dict], apps: list[dict], from_ts: str, to_ts: str) -> dict:
    now = datetime.now(UTC)
    now_iso = now.isoformat()
    week_ago = (now - timedelta(days=7)).isoformat()

    by_user: dict[str, dict] = {}
    for a in apps:  # apps arrive newest-first, so the first one seen is the last apply
        uid = a.get("user_id")
        if not uid:
            continue
        entry = by_user.setdefault(uid, {"count": 0, "last": a.get("date_applied")})
        entry["count"] += 1

    users = []
    for p in profiles:
        stats = by_user.get(p.get("user_id"), {})
        users.append(
            {
                "name": (p.get("name") or "").strip() or "(no name)",
                "signed_up": (p.get("created_at") or "")[:10],
                "signed_up_at": p.get("created_at") or "",
                "onboarded": "yes" if p.get("onboarding_completed") is True else "no",
                "tier": _effective_tier(p, now_iso),
                "source": _source_of(p.get("attribution")),
                "applications": stats.get("count", 0),
                "last_apply": (stats.get("last") or "")[:10] or "—",
                "last_apply_at": stats.get("last") or "",
            }
        )

    in_period = [u for u in users if from_ts <= u["signed_up_at"] <= to_ts]
    paying = [u for u in users if u["tier"] != "free"]
    never = [u for u in users if u["applications"] == 0]
    active7 = [u for u in users if u["last_apply_at"] and u["last_apply_at"] >= week_ago]
    onboarded = [u for u in users if u["onboarded"] == "yes"]

    roster = sorted(users, key=lambda u: (u["tier"] == "free", -u["applications"]))[:ROSTER_LIMIT]

    return {
        "key": "users",
        "title": "Accounts",
        "subtitle": "Who signed up, who pays, who actually uses it.",
        "metrics": [
            _metric(
                "paid_now",
                "Paying now",
                len(paying),
                scope="current",
                emphasis=True,
                description="Accounts on a paid tier that has not expired.",
            ),
            _metric("total_users", "Accounts", len(users), scope="all_time", emphasis=True),
            _metric("signups_period", "New signups", len(in_period)),
            _metric(
                "active_7d",
                "Active (7d)",
                len(active7),
                scope="current",
                description="Sent at least one application in the last 7 days.",
            ),
            _metric(
                "onboarded_share",
                "Onboarded",
                _pct(len(onboarded), len(users)),
                "percent",
                scope="all_time",
            ),
            _metric(
                "never_applied",
                "Never applied",
                len(never),
                scope="all_time",
                description="Signed up and never sent one application — the real activation gap.",
            ),
        ],
        "timeseries": {
            "label": "Signups per day",
            "format": "number",
            "points": _daily([u["signed_up_at"] for u in in_period]),
        },
        "tables": [
            _table(
                "roster",
                "Account roster (paying first)",
                [
                    _col("name", "Name"),
                    _col("tier", "Tier"),
                    _col("source", "Source"),
                    _col("signed_up", "Signed up", "date"),
                    _col("last_apply", "Last apply", "date"),
                    _col("onboarded", "Onboarded"),
                    _col("applications", "Apps", "number", "right"),
                ],
                [
                    {
                        k: u[k]
                        for k in (
                            "name",
                            "tier",
                            "source",
                            "signed_up",
                            "last_apply",
                            "onboarded",
                            "applications",
                        )
                    }
                    for u in roster
                ],
            ),
            _table(
                "by_source",
                "Signups by source (period)",
                [_col("source", "Source"), _col("count", "Signups", "number", "right")],
                [{"source": s, "count": c} for s, c in _tally([u["source"] for u in in_period])],
            ),
            _table(
                "by_tier",
                "Accounts by tier",
                [_col("tier", "Tier"), _col("count", "Accounts", "number", "right")],
                [{"tier": t, "count": c} for t, c in _tally([u["tier"] for u in users])],
            ),
        ],
    }


def _extension_users() -> set[str] | None:
    """Every user who has ever held an extension key. None when unreadable.

    Per USER, not per key: keys are revoked and re-issued on every reconnect, so
    counting rows read 30 keys from 5 people as a 79% connect rate (09-28).
    """
    try:
        return {
            r["user_id"]
            for r in _paged("extension_keys", "user_id", ("created_at",))
            if r.get("user_id")
        }
    except Exception as exc:  # noqa: BLE001
        print(f"[admin.funnel] extension_keys unavailable: {exc}", file=sys.stderr)
        return None


def _section_funnel(profiles: list[dict], apps: list[dict], from_ts: str, to_ts: str) -> dict:
    """Every step is a SUBSET OF PERIOD SIGNUPS — the only way the percentages
    stay <= 100% and mean "of the people who signed up in this window".

    Activation is "sent a first application" (a row in `applications`), not
    "started a campaign": campaign_states.started_at is cleared on Stop, so
    that step read 0 while 126 applications existed (09-28).
    """
    now_iso = datetime.now(UTC).isoformat()
    signups = [p for p in profiles if from_ts <= (p.get("created_at") or "") <= to_ts]
    signup_ids = {p["user_id"] for p in signups if p.get("user_id")}
    onboarded = [p for p in signups if p.get("onboarding_completed") is True]
    key_users = _extension_users()
    connected = len(signup_ids & key_users) if key_users is not None else None
    applied_users = {a["user_id"] for a in apps if a.get("user_id")}
    first_app = len(signup_ids & applied_users)
    submitted = len([a for a in apps if from_ts <= (a.get("date_applied") or "") <= to_ts])
    paid_now = _count("profiles", subscription_expires_at__gt=now_iso)

    steps = [
        {"step": "Signed up", "count": len(signups), "of_signups": 100.0 if signups else None},
        {
            "step": "Completed onboarding",
            "count": len(onboarded),
            "of_signups": _pct(len(onboarded), len(signups)),
        },
        {
            "step": "Connected extension",
            "count": connected,
            "of_signups": _pct(connected, len(signups)),
        },
        {
            "step": "Sent first application",
            "count": first_app,
            "of_signups": _pct(first_app, len(signups)),
        },
    ]

    return {
        "key": "funnel",
        "title": "Funnel",
        "subtitle": (
            "Signup → onboarding → extension → first application. Every step counts "
            "only the people who signed up in this period."
        ),
        "metrics": [
            _metric("signups", "Signups", len(signups), emphasis=True),
            _metric(
                "first_application",
                "Sent first application",
                first_app,
                description="Period signups with at least one application, ever — the real activation.",
            ),
            _metric("applications", "Applications sent", submitted),
            _metric(
                "paid_users",
                "Paid users",
                paid_now,
                scope="current",
                emphasis=True,
                description="Profiles on a paid tier right now (not period-scoped).",
            ),
            _metric(
                "activation_rate",
                "Signup → first application",
                _pct(first_app, len(signups)),
                "percent",
                description="Share of period signups that have sent at least one application.",
            ),
            _metric(
                "extension_rate",
                "Extension connect rate",
                _pct(connected, len(signups)),
                "percent",
                description=(
                    "Share of period signups who ever connected the extension — counted per "
                    "person, not per key (keys are re-issued on every reconnect)."
                    if connected is not None
                    else "extension_keys could not be read."
                ),
            ),
        ],
        "timeseries": {
            "label": "Signups per day",
            "format": "number",
            "points": _daily([p.get("created_at") or "" for p in signups]),
        },
        "tables": [
            _table(
                "steps",
                "Funnel steps",
                [
                    _col("step", "Step"),
                    _col("count", "Count", "number", "right"),
                    _col("of_signups", "% of signups", "percent", "right"),
                ],
                steps,
            )
        ],
    }


# --- ads ---------------------------------------------------------------------
# Spend comes from `ad_spend` (Meta Insights sync, Google Ads Script, manual
# CLI); signups/activation/paying come from our own tables. The join key is
# utm_content = the ad id, set on every ad URL (docs/handoff/ads-board.md).

_PLATFORM_CHANNEL = {"meta": attr_rules.META_PAID, "google": attr_rules.GOOGLE_PAID}
_CHANNEL_PLATFORM = {v: k for k, v in _PLATFORM_CHANNEL.items()}


def _ads_budget() -> float:
    try:
        return float(os.getenv("ADS_MONTHLY_BUDGET_USD", "") or DEFAULT_ADS_MONTHLY_BUDGET_USD)
    except ValueError:
        return DEFAULT_ADS_MONTHLY_BUDGET_USD


def _spend_channel(row: dict) -> str:
    """Which signup channel a spend row bought. Manual rows carry the utm_source
    they paid for in account_id (e.g. `reddit`)."""
    platform = row.get("platform")
    if platform in _PLATFORM_CHANNEL:
        return _PLATFORM_CHANNEL[platform]
    return (row.get("account_id") or "manual").strip().lower() or "manual"


def _money(value: float) -> float:
    return round(value, 2)


def _ratio(num: float | None, den: float | None) -> float | None:
    """num/den to cents, or None when either side is missing or den is zero —
    "cost per signup" with no signups is unknown, not $0 and not infinite."""
    if num is None or not den:
        return None
    return round(num / den, 2)


def _sum_or_none(values: list) -> int | None:
    present = [int(v) for v in values if v is not None]
    return sum(present) if present else None


def _paid_users(profiles: list[dict], since_ts: str) -> tuple[dict[str, int] | None, str]:
    """({user_id: net cents collected}, how it was decided).

    "Paid" = at least one SUCCEEDED Stripe charge, net of refunds, on the
    Stripe customer our webhook linked to the user (profiles.stripe_customer_id,
    written on checkout.session.completed). Not `subscription_tier`: promo codes
    write that column too, and an expired sub reads `free` — neither is money.
    Charges since the period start cover every period signup, who cannot have
    paid before signing up.

    Fallback when Stripe can't be read: a linked customer (= a completed
    checkout) counts as paid and revenue is unknown (None in the map's place).
    """
    by_customer = {
        p["stripe_customer_id"]: p["user_id"] for p in profiles if p.get("stripe_customer_id")
    }
    stripe = _stripe_client()
    reason = "STRIPE_SECRET_KEY is not set"
    if stripe is not None:
        try:
            net: dict[str, int] = defaultdict(int)
            for ch in _paid_charges(stripe, _epoch(since_ts)):
                uid = by_customer.get(ch["customer"])
                if uid:
                    net[uid] += ch["amount"] - ch["refunded"]
            return {u: c for u, c in net.items() if c > 0}, (
                "Stripe: at least one succeeded charge, net of refunds, on the user's linked customer."
            )
        except Exception as exc:  # noqa: BLE001
            reason = f"Stripe unreadable ({type(exc).__name__})"
            print(f"[admin.ads] {reason}: {exc}", file=sys.stderr)
    # Fallback: presence of a linked customer. Revenue unknown -> None.
    return (
        None,
        f"{reason} — counting a completed checkout (linked Stripe customer) as paid; revenue unknown.",
    )


def _section_ads(
    profiles: list[dict], apps: list[dict], from_ts: str, to_ts: str, from_day: str, to_day: str
) -> dict:
    today = datetime.now(UTC).date()
    month_start = today.replace(day=1).isoformat()
    today_s = today.isoformat()
    ceiling = ad_rules.cac_ceiling()
    budget = _ads_budget()

    # 1. Spend: refresh Meta if stale (never raises), then read every row.
    meta_state = meta_spend.sync_if_stale()
    spend_error = None
    try:
        spend_rows = spend_db.read_all()
    except Exception as exc:  # noqa: BLE001
        spend_rows = []
        # PostgREST errors carry a readable `.message`; str() is a dict dump.
        detail = getattr(exc, "message", None) or str(exc)
        spend_error = f"ad_spend unreadable: {detail}"[:300]
        print(f"[admin.ads] {type(exc).__name__}: {exc}", file=sys.stderr)

    has_rows = {p: any(r.get("platform") == p for r in spend_rows) for p in spend_db.PLATFORMS}
    # Meta counts as a source once it has given us data, or a sync succeeded
    # (with nothing delivered yet an honest $0). Configured-but-failing with no
    # rows is NOT a source: its spend is unknown, not zero.
    meta_connected = has_rows["meta"] or (meta_state["connected"] and not meta_state["error"])
    # Google has no pull: it is "connected" once the Ads Script has posted.
    connected = {"meta": meta_connected, "google": has_rows["google"], "manual": has_rows["manual"]}
    any_source = spend_error is None and any(connected.values())

    period_rows = [r for r in spend_rows if from_day <= str(r.get("date")) <= to_day]
    mtd_rows = [r for r in spend_rows if month_start <= str(r.get("date")) <= today_s]

    def spend_of(rows: list[dict]) -> float:
        return _money(sum(float(r.get("spend_usd") or 0) for r in rows))

    spend_period = spend_of(period_rows) if any_source else None
    spend_mtd = spend_of(mtd_rows) if any_source else None

    # 2. Who signed up, from where, and what they did since.
    applied = {a["user_id"] for a in apps if a.get("user_id")}
    paid_cents, paid_how = _paid_users(profiles, from_ts)
    signups = []
    for p in profiles:
        if not (from_ts <= (p.get("created_at") or "") <= to_ts) or not p.get("user_id"):
            continue
        attribution = p.get("attribution") if isinstance(p.get("attribution"), dict) else {}
        uid = p["user_id"]
        if paid_cents is None:
            paid = bool(p.get("stripe_customer_id"))
            revenue = None
        else:
            paid = uid in paid_cents
            revenue = paid_cents.get(uid, 0) / 100
        signups.append(
            {
                "user_id": uid,
                "signed_up": (p.get("created_at") or "")[:10],
                "channel": attr_rules.channel_of(attribution).strip().lower() or "direct",
                "utm_campaign": attribution.get("utm_campaign"),
                "utm_content": str(attribution.get("utm_content") or "").strip() or None,
                "activated": uid in applied,
                "paid": paid,
                "revenue": revenue,
            }
        )

    manual_channels = {_spend_channel(r) for r in period_rows if r.get("platform") == "manual"}
    paid_channels = set(attr_rules.PAID_AD_CHANNELS) | manual_channels
    ad_signups = [s for s in signups if s["channel"] in paid_channels]
    n_signups = len(ad_signups)
    n_activated = sum(1 for s in ad_signups if s["activated"])
    n_paying = sum(1 for s in ad_signups if s["paid"])
    ad_revenue = None if paid_cents is None else _money(sum(s["revenue"] or 0 for s in ad_signups))

    # 3. Say what is (not) connected — in the descriptions, never as a zero.
    source_notes = []
    if spend_error:
        source_notes.append(spend_error)
    elif meta_state.get("error"):
        source_notes.append(meta_state["error"])
    if spend_error is None and not connected["google"]:
        source_notes.append(
            "Google not connected: paste scripts/google_ads_spend.js into Google Ads"
        )
    if spend_error:
        no_spend_reason = f"{spend_error} (is migrations/2026-09-28_ad_spend.sql applied?)"
    elif not any_source:
        no_spend_reason = "No spend source connected. " + "; ".join(source_notes)
    else:
        no_spend_reason = None
    spend_desc = no_spend_reason or (
        "Ad spend in this period across "
        + ", ".join(p for p, ok in connected.items() if ok)
        + (f". {'; '.join(source_notes)}." if source_notes else ".")
    )

    def cost_desc(what: str, den: int) -> str | None:
        if spend_period is None:
            return no_spend_reason
        if not den:
            return f"No {what} from ads in this period yet — cost unknown, not $0."
        return None

    metrics = [
        _metric("spend", "Spend", spend_period, "currency", emphasis=True, description=spend_desc),
        _metric(
            "budget_used",
            "Budget used this month",
            _pct(spend_mtd, budget) if spend_mtd is not None else None,
            "percent",
            scope="current",
            emphasis=True,
            description=(
                f"${spend_mtd:,.2f} of ${budget:,.0f} spent since {month_start} "
                "(ADS_MONTHLY_BUDGET_USD)."
                if spend_mtd is not None
                else no_spend_reason
            ),
        ),
        _metric(
            "paid_signups",
            "Paid-ad signups",
            n_signups,
            emphasis=True,
            description="Period signups whose first touch was a paid ad (Meta/Google UTMs or a Google click id).",
        ),
        _metric(
            "cost_per_signup",
            "Cost per signup",
            _ratio(spend_period, n_signups),
            "currency",
            description=cost_desc("signups", n_signups),
        ),
        _metric(
            "activated_from_ads",
            "Activated from ads",
            n_activated,
            description="Paid-ad signups who have sent at least one application.",
        ),
        _metric(
            "cost_per_activation",
            "Cost per activation",
            _ratio(spend_period, n_activated),
            "currency",
            description=cost_desc("activations", n_activated),
        ),
        _metric("paying_from_ads", "Paying from ads", n_paying, description=paid_how),
        _metric(
            "cac",
            "CAC",
            _ratio(spend_period, n_paying),
            "currency",
            emphasis=True,
            description=cost_desc("paying users", n_paying)
            or f"Spend / paying users. Ceiling ${ceiling:.0f} (ADS_CAC_CEILING_USD).",
        ),
        _metric(
            "roas",
            "ROAS",
            _ratio(ad_revenue, spend_period),
            description=(
                "Revenue collected from paid-ad signups / spend."
                if ad_revenue is not None and spend_period
                else (
                    no_spend_reason
                    or ("Revenue unknown: " + paid_how if ad_revenue is None else "No spend yet.")
                )
            ),
        ),
    ]

    # 4. By channel — every channel, organic included, so paid is read against it.
    by_channel: dict[str, dict] = {}
    for s in signups:
        c = by_channel.setdefault(s["channel"], {"signups": 0, "activated": 0, "paid": 0})
        c["signups"] += 1
        c["activated"] += int(s["activated"])
        c["paid"] += int(s["paid"])
    for ch in paid_channels:
        by_channel.setdefault(ch, {"signups": 0, "activated": 0, "paid": 0})

    def channel_connected(ch: str) -> bool:
        if spend_error:
            return False
        platform = _CHANNEL_PLATFORM.get(ch)
        return connected[platform] if platform else ch in manual_channels

    channel_rows = []
    for ch, c in by_channel.items():
        rows_for = [r for r in period_rows if _spend_channel(r) == ch]
        spend = spend_of(rows_for) if ch in paid_channels and channel_connected(ch) else None
        channel_rows.append(
            {
                "channel": ch,
                "spend": spend,
                "clicks": _sum_or_none([r.get("clicks") for r in rows_for])
                if spend is not None
                else None,
                "signups": c["signups"],
                "activated": c["activated"],
                "paid": c["paid"],
                "cost_signup": _ratio(spend, c["signups"]),
                "cost_activation": _ratio(spend, c["activated"]),
                "cac": _ratio(spend, c["paid"]),
            }
        )
    channel_rows.sort(
        key=lambda r: (r["channel"] not in paid_channels, -r["signups"], r["channel"])
    )

    # 5. By ad (Meta + Google; manual spend has no ad-level join).
    known_ads = {
        (r.get("platform"), str(r.get("ad_id")))
        for r in spend_rows
        if r.get("platform") != "manual"
    }
    by_ad: dict[tuple, dict] = {}
    for r in period_rows:
        if r.get("platform") == "manual":
            continue
        key = (r["platform"], str(r.get("ad_id")))
        a = by_ad.setdefault(
            key,
            {"campaign": None, "ad": None, "spend": 0.0, "impressions": [], "clicks": []},
        )
        a["campaign"] = r.get("campaign_name") or r.get("campaign_id") or a["campaign"]
        a["ad"] = r.get("ad_name") or a["ad"]
        a["spend"] += float(r.get("spend_usd") or 0)
        a["impressions"].append(r.get("impressions"))
        a["clicks"].append(r.get("clicks"))

    ad_rows = []
    for (platform, ad_id), a in by_ad.items():
        channel = _PLATFORM_CHANNEL[platform]
        mine = [s for s in ad_signups if s["channel"] == channel and s["utm_content"] == ad_id]
        spend = _money(a["spend"])
        impressions = _sum_or_none(a["impressions"])
        clicks = _sum_or_none(a["clicks"])
        n_s = len(mine)
        n_a = sum(1 for s in mine if s["activated"])
        n_p = sum(1 for s in mine if s["paid"])
        ad_rows.append(
            {
                "platform": platform,
                "campaign": a["campaign"] or "—",
                "ad": a["ad"] or ad_id,
                "spend": spend,
                "impressions": impressions,
                "clicks": clicks,
                "ctr": ad_rules.ctr_pct(clicks, impressions),
                "cpc": _ratio(spend, clicks),
                "signups": n_s,
                "activated": n_a,
                "paid": n_p,
                "verdict": ad_rules.ad_verdict(spend, impressions, clicks, n_s, n_a, n_p, ceiling),
            }
        )
    ad_rows.sort(key=lambda r: -r["spend"])

    # 6. Unmatched — paid-ad signups we cannot tie to an ad: a broken tracking
    # template shows up here instead of as a quietly low signup count.
    unmatched = []
    for s in ad_signups:
        platform = _CHANNEL_PLATFORM.get(s["channel"])
        if platform is None:  # manual channels have no ad-level join
            continue
        if (platform, s["utm_content"]) in known_ads:
            continue
        if not s["utm_content"]:
            why = "no utm_content on the landing URL (check the ad's URL parameters)"
        elif not connected[platform]:
            why = f"{platform} spend not connected"
        else:
            why = "utm_content matches no ad in ad_spend (not synced yet, or a wrong UTM)"
        unmatched.append(
            {
                "signed_up": s["signed_up"],
                "account": s["user_id"][:8],
                "channel": s["channel"],
                "utm_campaign": s["utm_campaign"] or "—",
                "utm_content": s["utm_content"] or "—",
                "why": why,
            }
        )

    # 7. Spend sources — what is connected and how fresh it is.
    def newest(platform: str) -> str | None:
        stamps = [
            str(r.get("synced_at") or "") for r in spend_rows if r.get("platform") == platform
        ]
        return max(stamps)[:16].replace("T", " ") if stamps else None

    source_rows = []
    for platform in spend_db.PLATFORMS:
        if spend_error:
            status = "ad_spend unreadable (see Spend)"
        elif platform == "meta":
            status = meta_state.get("error") or (
                (
                    "connected (ads-manager agent, Meta MCP)"
                    if meta_state.get("via") == "mcp"
                    else "connected"
                )
                if meta_connected
                else meta_spend.NOT_CONNECTED
            )
        elif platform == "google":
            status = (
                "posting"
                if connected["google"]
                else "not connected — install scripts/google_ads_spend.js"
            )
        else:
            status = "has rows" if connected["manual"] else "none (scripts/ads_spend.py add)"
        source_rows.append(
            {
                "platform": platform,
                "status": status,
                "last_sync": newest(platform),
                "rows": sum(1 for r in spend_rows if r.get("platform") == platform),
                "spend": spend_of([r for r in period_rows if r.get("platform") == platform])
                if connected[platform] and not spend_error
                else None,
            }
        )

    by_date: dict[str, float] = defaultdict(float)
    for r in period_rows:
        by_date[str(r.get("date"))] += float(r.get("spend_usd") or 0)

    return {
        "key": "ads",
        "title": "Ads",
        "subtitle": (
            "Paid acquisition: spend from the ad platforms against signups, activation and "
            "payment from our own tables, joined on utm_content = ad id."
        ),
        "metrics": metrics,
        "timeseries": {
            "label": "Ad spend per day",
            "format": "currency",
            "points": [{"date": d, "value": _money(v)} for d, v in sorted(by_date.items())],
        },
        "tables": [
            _table(
                "by_channel",
                "By channel (period signups)",
                [
                    _col("channel", "Channel"),
                    _col("spend", "Spend", "currency", "right"),
                    _col("clicks", "Clicks", "number", "right"),
                    _col("signups", "Signups", "number", "right"),
                    _col("activated", "Activated", "number", "right"),
                    _col("paid", "Paid", "number", "right"),
                    _col("cost_signup", "Cost/signup", "currency", "right"),
                    _col("cost_activation", "Cost/activation", "currency", "right"),
                    _col("cac", "CAC", "currency", "right"),
                ],
                channel_rows,
            ),
            _table(
                "by_ad",
                "By ad",
                [
                    _col("platform", "Platform"),
                    _col("campaign", "Campaign"),
                    _col("ad", "Ad"),
                    _col("spend", "Spend", "currency", "right"),
                    _col("impressions", "Impr.", "number", "right"),
                    _col("clicks", "Clicks", "number", "right"),
                    _col("ctr", "CTR", "percent", "right"),
                    _col("cpc", "CPC", "currency", "right"),
                    _col("signups", "Signups", "number", "right"),
                    _col("activated", "Activated", "number", "right"),
                    _col("paid", "Paid", "number", "right"),
                    _col("verdict", "Verdict"),
                ],
                ad_rows,
            ),
            _table(
                "unmatched",
                "Paid-ad signups not matched to an ad",
                [
                    _col("signed_up", "Signed up", "date"),
                    _col("account", "Account"),
                    _col("channel", "Channel"),
                    _col("utm_campaign", "utm_campaign"),
                    _col("utm_content", "utm_content"),
                    _col("why", "Why"),
                ],
                unmatched,
            ),
            _table(
                "sources",
                "Spend sources",
                [
                    _col("platform", "Platform"),
                    _col("status", "Status"),
                    _col("last_sync", "Last sync (UTC)"),
                    _col("rows", "Rows", "number", "right"),
                    _col("spend", "Spend (period)", "currency", "right"),
                ],
                source_rows,
            ),
        ],
    }


def _section_ai_cost(
    apps: list[dict], from_ts: str, to_ts: str, from_day: str, to_day: str
) -> dict:
    # Dollars come from the ledger: every Anthropic call priced from its own token counts
    # (modules/ai_meter.py). scripts/ai_cost_report.py computes the same numbers with the
    # same function, so the board and the daily report cannot disagree.
    start = ai_calls_db.first_at()
    s = ai_spend.summarize(ai_calls_db.daily(from_day, to_day), apps, from_day, to_day, start)
    ceiling = ai_spend.CEILING_PER_APPLICATION_USD
    spenders = [a for a in s["by_account"] if a["user_id"] is not None and a["cost"] > 0]

    metrics = [
        _metric(
            "ai_spend",
            "AI spend",
            round(s["cost"], 2),
            "currency",
            emphasis=True,
            description="Every Anthropic call, priced from its own token counts.",
        ),
        _metric("ai_calls", "AI calls", s["calls"], emphasis=True),
        _metric("applications", "Applications", s["applications"]),
        _metric(
            "cost_per_application",
            "Cost / application",
            s["per_application"],
            "currency",
            emphasis=True,
            description=f"Ceiling ${ceiling}: the monthly price after the affiliate share, "
            "over every application the paid cap allows in a month.",
        ),
    ]
    if s["calls_per_application"] is not None:
        metrics.append(
            _metric("calls_per_application", "Calls / application", s["calls_per_application"])
        )
    metrics.append(_metric("active_spenders", "Accounts spending", len(spenders)))
    if spenders:
        monthly = PLANS["monthly"]["price_usd"]
        left = monthly * (1 - ai_spend.AFFILIATE_RATE) - s["cost"] / len(spenders)
        metrics.append(
            _metric(
                "margin_per_paying_user",
                f"Left after AI + {int(ai_spend.AFFILIATE_RATE * 100)}%",
                round(left, 2),
                "currency",
                description=f"${monthly} monthly minus the affiliate share minus this period's AI cost per active account.",
            )
        )
    metrics.append(
        _metric(
            "unattributed_spend",
            "Charged to no account",
            round(s["unattributed_cost"], 2),
            "currency",
            description="Calls made outside a request with no user bound (scripts, a thread "
            "missing ai_meter.attributed).",
        )
    )
    if s["cache_read_share"] is not None:
        metrics.append(
            _metric(
                "cache_read_share",
                "Prompt from cache",
                round(s["cache_read_share"] * 100, 1),
                "percent",
            )
        )

    return {
        "key": "ai_cost",
        "title": "AI cost",
        "subtitle": (
            f"Metered since {start[:10]}; applications before that are left out."
            if start
            else "No AI call has been recorded yet."
        ),
        "metrics": metrics,
        "timeseries": {
            "label": "AI spend per day",
            "format": "currency",
            "points": [{"date": d["day"], "value": round(d["cost"], 2)} for d in s["days"]],
        },
        "tables": [
            _table(
                "spenders",
                "Cost by account",
                [
                    _col("account", "Account"),
                    _col("applications", "Apps", "number", "right"),
                    _col("calls", "Calls", "number", "right"),
                    _col("cost", "Cost", "currency", "right"),
                    _col("cost_per_app", "Cost/app", "currency", "right"),
                ],
                [
                    {
                        # A short id only: cost shape belongs here, names belong in the roster.
                        "account": str(a["user_id"])[:8],
                        "applications": a["applications"],
                        "calls": a["calls"],
                        "cost": round(a["cost"], 2),
                        "cost_per_app": a["per_application"],
                    }
                    for a in spenders[:SPENDER_LIMIT]
                ],
            ),
            _table(
                "purposes",
                "Cost by purpose",
                [
                    _col("purpose", "Purpose"),
                    _col("calls", "Calls", "number", "right"),
                    _col("cost", "Cost", "currency", "right"),
                ],
                [
                    {"purpose": p["purpose"], "calls": p["calls"], "cost": round(p["cost"], 2)}
                    for p in s["by_purpose"]
                ],
            ),
        ],
    }


def _section_buddy(from_ts: str, to_ts: str) -> dict:
    """Drop, the support chat: how much it's used, what it costs, where it falls short.

    Same formula as scripts/buddy_review.py (app/db/buddy_log.summarize). Flags are plain
    rules over the logged turns — failed, didn't know, re-asked within 3 min, too long,
    lookup failed, 👎 — so the board costs nothing to open and every flag explains itself.
    """
    from app.db import buddy_log

    turns, feedback, limits = buddy_log.read(from_ts, to_ts)
    s = buddy_log.summarize(turns, feedback, limits)
    n = s["questions"] or 0

    def pct(x: int) -> float | None:
        return round(100 * x / n, 1) if n else None

    metrics = [
        _metric("questions", "Questions", n, emphasis=True),
        _metric("askers", "People asking", s["askers"]),
        _metric("per_asker", "Questions / person", s["per_asker"]),
        _metric(
            "cost",
            "Drop spend",
            s["cost_usd"],
            "currency",
            emphasis=True,
            description="Summed from each answer's logged token counts x list price.",
        ),
        _metric("cost_per_answer", "Cost / answer", s["cost_per_answer"], "currency"),
        _metric(
            "didnt_know",
            "Didn't know",
            pct(s["didnt_know"]),
            "percent",
            description="Answers that sent the person to support or said 'not sure' — "
            "a gap in Drop's tools or facts when it repeats.",
        ),
        _metric(
            "asked_again",
            "Re-asked within 3 min",
            pct(s["asked_again"]),
            "percent",
            description="The same person asked about the same thing again right away: "
            "the first answer didn't land.",
        ),
        _metric("failed", "Failed answers", s["failed"]),
        _metric("thumbs", "👍 / 👎", f"{s['thumbs_up']} / {s['thumbs_down']}", "text"),
        _metric(
            "cards",
            "Cards pressed / shown",
            f"{s['cards_pressed']} / {s['cards_shown']}",
            "text",
            description="Drop proposes, the person presses (remember an answer, open an "
            "application, rebuild the resume…).",
        ),
        _metric(
            "latency",
            "Answer time p50 / p95",
            f"{s['latency_p50_s']}s / {s['latency_p95_s']}s",
            "text",
        ),
        _metric(
            "limit_hits",
            "Hit the daily cap",
            s["limit_hitters"],
            description=f"People refused by the 20/day cap ({s['limit_hits']} refusals).",
        ),
    ]
    return {
        "key": "buddy",
        "title": "Drop (support chat)",
        "subtitle": "Every answer is logged with its cost; flags are rules, not a model's opinion.",
        "metrics": metrics,
        "timeseries": {
            "label": "Questions per day",
            "format": "number",
            "points": [{"date": d, "value": v} for d, v in s["by_day"]],
        },
        "tables": [
            _table(
                "flagged",
                f"Answers to read ({s['flagged_total']} flagged)",
                [
                    _col("at", "When"),
                    _col("account", "Account"),
                    _col("flags", "Why"),
                    _col("question", "Question"),
                    _col("answer", "Answer"),
                ],
                s["flagged"],
            ),
            _table(
                "cards",
                "Cards by kind",
                [
                    _col("kind", "Kind"),
                    _col("shown", "Shown", "number", "right"),
                    _col("pressed", "Pressed", "number", "right"),
                ],
                s["cards_by_kind"],
            ),
            _table(
                "tools",
                "Lookups",
                [_col("tool", "Tool"), _col("calls", "Calls", "number", "right")],
                s["tools"],
            ),
        ],
    }


def _section_affiliates(from_ts: str, to_ts: str) -> dict:
    payable_cutoff = (datetime.now(UTC) - timedelta(days=REFUND_WINDOW_DAYS)).isoformat()

    affiliates = _rows("affiliates", "id, code, status, commission_pct, paypal_email")
    referrals = _rows("referrals", "affiliate_id, first_seen_at, first_paid_at")
    # The whole ledger, not just the period: "owed" is an all-time liability.
    commissions = _rows("commissions", "affiliate_id, amount_cents, status, created_at, payout_id")
    payouts = _rows("payouts", "affiliate_id, amount_cents, paid_at")
    # The inbox: people asking for a link. Approval is a human decision made
    # on this board (POST /admin/affiliates/decide), never automatic.
    # Aggregated server-side: click rows are the one table here that outgrows
    # PostgREST's silent 1000-row ceiling, so counting them in Python would
    # quietly plateau. Failure degrades to "no clicks column", never to a wrong
    # number.
    clicks_by_code: dict[str, dict] = {}
    try:
        res = (
            get_supabase()
            .rpc("affiliate_click_totals", {"p_from": from_ts, "p_to": to_ts})
            .execute()
        )
        clicks_by_code = {r["code"]: r for r in (res.data or [])}
    except Exception as exc:  # noqa: BLE001
        print(f"[admin.affiliates] click totals unavailable: {exc}", file=sys.stderr)

    applications = _rows(
        "affiliate_applications",
        "id, email, name, desired_code, audience, audience_size, promo_plan, "
        "paypal_email, source, status, created_at, affiliate_id",
        limit=500,
    )

    def in_period(ts: str | None) -> bool:
        return bool(ts) and from_ts <= ts <= to_ts

    live = [c for c in commissions if c.get("status") != "reversed"]
    accrued = [c for c in commissions if c.get("status") == "accrued"]
    # Still 'accrued' but already bundled into a Connect payout
    # (scripts/run_affiliate_payouts.py claims BEFORE Stripe confirms) is money
    # that would be sent twice if this board's "payable" led Igor to also send
    # it by hand — "owed" below is a lifetime liability and rightly still
    # counts it, "payable" must not (blast-radius review, 2026-10-01).
    accrued_unclaimed = [c for c in accrued if not c.get("payout_id")]

    earned_period = sum(c["amount_cents"] for c in live if in_period(c.get("created_at")))
    reversed_period = sum(
        c["amount_cents"]
        for c in commissions
        if c.get("status") == "reversed" and in_period(c.get("created_at"))
    )
    owed_total = sum(c["amount_cents"] for c in accrued)
    payable_now = sum(
        c["amount_cents"] for c in accrued_unclaimed if (c.get("created_at") or "") < payable_cutoff
    )
    paid_period = sum(p["amount_cents"] for p in payouts if in_period(p.get("paid_at")))

    rows = []
    for a in affiliates:
        mine = [r for r in referrals if r.get("affiliate_id") == a["id"]]
        cs = [c for c in commissions if c.get("affiliate_id") == a["id"]]
        mine_accrued = [c for c in cs if c.get("status") == "accrued"]
        mine_unclaimed = [c for c in mine_accrued if not c.get("payout_id")]
        rows.append(
            {
                "code": a.get("code"),
                # The whole link, not just the code: the board's approval result
                # is gone once you close it, and a partner who asks "what was my
                # link again?" should be one copy away, not a reconstruction.
                "link": f"{FRONTEND_URL}/?ref={a.get('code')}",
                "status": a.get("status"),
                "rate": f"{round(float(a.get('commission_pct') or 0))}%",
                "clicks": int((clicks_by_code.get(a.get("code")) or {}).get("clicks_total") or 0),
                "signups": len(mine),
                "paying": len([r for r in mine if r.get("first_paid_at")]),
                "earned": _usd(sum(c["amount_cents"] for c in cs if c.get("status") != "reversed")),
                "owed": _usd(sum(c["amount_cents"] for c in mine_accrued)),
                # Excludes anything already claimed by an in-flight Connect payout
                # (payout_id set, status still 'accrued') — paying that by hand
                # too would be the same money twice.
                "payable": _usd(
                    sum(
                        c["amount_cents"]
                        for c in mine_unclaimed
                        if (c.get("created_at") or "") < payable_cutoff
                    )
                ),
                # Text, because it is a to-do and not a number: a partner with
                # money owed and no PayPal address cannot be paid.
                "paypal": "missing" if not a.get("paypal_email") else "on file",
            }
        )
    rows.sort(key=lambda r: (-r["earned"], -r["signups"]))

    by_date: dict[str, int] = defaultdict(int)
    for c in live:
        if in_period(c.get("created_at")):
            by_date[c["created_at"][:10]] += c["amount_cents"]

    return {
        "key": "affiliates",
        "title": "Affiliates",
        "subtitle": "What we owe partners, and what can actually be sent today.",
        "metrics": [
            _metric(
                "payable_now",
                "Ready to pay out",
                _usd(payable_now),
                "currency",
                scope="current",
                emphasis=True,
                description=f"Commission past the {REFUND_WINDOW_DAYS}-day refund window — payable today.",
            ),
            _metric(
                "owed_total",
                "Owed (incl. holding)",
                _usd(owed_total),
                "currency",
                scope="all_time",
                emphasis=True,
                description="Every accrued, unpaid commission — including what is still inside the refund window.",
            ),
            _metric("earned_period", "Commission accrued", _usd(earned_period), "currency"),
            _metric(
                "paid_period",
                "Paid out",
                _usd(paid_period),
                "currency",
                description="Payouts recorded in this period (entered by hand after PayPal).",
            ),
            _metric(
                "active_affiliates",
                "Active partners",
                len([a for a in affiliates if a.get("status") == "active"]),
                scope="current",
            ),
            _metric(
                "link_opens",
                "Link opens",
                sum(int(c.get("clicks_period") or 0) for c in clicks_by_code.values()),
                description="Unique visitors per day on any ?ref= link, partner codes and printed codes alike.",
            ),
            _metric(
                "referred_signups",
                "Referred signups",
                len([r for r in referrals if in_period(r.get("first_seen_at"))]),
            ),
            _metric(
                "paying_referrals",
                "Referrals paying",
                len([r for r in referrals if r.get("first_paid_at")]),
                scope="current",
            ),
            _metric(
                "reversed_period",
                "Clawed back",
                _usd(reversed_period),
                "currency",
                description="Commission reversed by customer refunds in this period.",
            ),
            _metric(
                "applications_waiting",
                "Applications waiting",
                len([a for a in applications if a.get("status") == "new"]),
                scope="current",
                emphasis=True,
                description="People who asked for a link and are waiting on a decision.",
            ),
        ],
        "timeseries": {
            "label": "Commission accrued per day",
            "format": "currency",
            "points": [{"date": d, "value": _usd(v)} for d, v in sorted(by_date.items())],
        },
        "tables": [
            _table(
                "applications",
                "Applications waiting",
                [
                    _col("name", "Name"),
                    _col("email", "Email"),
                    _col("desired_code", "Wants link"),
                    _col("audience", "Audience"),
                    _col("audience_size", "Size"),
                    _col("promo_plan", "How they'll share"),
                    _col("paypal", "PayPal"),
                    _col("source", "Came from"),
                    _col("applied", "Applied", "date"),
                    _col("id", "id"),
                ],
                [
                    {
                        "name": a.get("name"),
                        "email": a.get("email"),
                        "desired_code": a.get("desired_code"),
                        "audience": a.get("audience") or "—",
                        "audience_size": a.get("audience_size") or "—",
                        "promo_plan": a.get("promo_plan") or "—",
                        "paypal": "missing" if not a.get("paypal_email") else a.get("paypal_email"),
                        "source": a.get("source") or "direct",
                        "applied": (a.get("created_at") or "")[:10],
                        # Carried so the board's Approve button knows what to act on.
                        "id": a.get("id"),
                    }
                    for a in sorted(
                        [a for a in applications if a.get("status") == "new"],
                        key=lambda a: a.get("created_at") or "",
                    )
                ],
            ),
            _table(
                "decided",
                "Decided applications",
                [
                    _col("name", "Name"),
                    _col("desired_code", "Link"),
                    _col("status", "Decision"),
                    _col("linked", "Live"),
                    _col("applied", "Applied", "date"),
                ],
                [
                    {
                        "name": a.get("name"),
                        "desired_code": a.get("desired_code"),
                        "status": a.get("status"),
                        # Approved but not linked = the code is reserved and goes
                        # live when that email signs up.
                        "linked": "yes" if a.get("affiliate_id") else "reserved",
                        "applied": (a.get("created_at") or "")[:10],
                    }
                    for a in sorted(
                        [a for a in applications if a.get("status") != "new"],
                        key=lambda a: a.get("created_at") or "",
                        reverse=True,
                    )
                ],
            ),
            _table(
                "unclaimed_codes",
                "Codes with traffic and no partner",
                [
                    _col("code", "Code"),
                    _col("clicks", "Opens", "number", "right"),
                    _col("clicks_period", "This period", "number", "right"),
                ],
                # The printed material: ?ref=card vs ?ref=stka vs ?ref=stkb.
                # Nobody earns from these — they answer "which artefact gets
                # scanned", which was unanswerable before clicks existed.
                sorted(
                    [
                        {
                            "code": code,
                            "clicks": int(c.get("clicks_total") or 0),
                            "clicks_period": int(c.get("clicks_period") or 0),
                        }
                        for code, c in clicks_by_code.items()
                        if code not in {a.get("code") for a in affiliates}
                    ],
                    key=lambda r: -r["clicks"],
                ),
            ),
            _table(
                "partners",
                "Partner ledger",
                [
                    _col("code", "Code"),
                    _col("link", "Link"),
                    _col("status", "Status"),
                    _col("rate", "Rate"),
                    _col("clicks", "Opens", "number", "right"),
                    _col("signups", "Signups", "number", "right"),
                    _col("paying", "Paying", "number", "right"),
                    _col("earned", "Earned", "currency", "right"),
                    _col("owed", "Owed", "currency", "right"),
                    _col("payable", "Payable", "currency", "right"),
                    _col("paypal", "PayPal"),
                ],
                rows,
            ),
        ],
    }


def _stripe_client():
    """The configured Stripe SDK, or None when STRIPE_SECRET_KEY is unset."""
    from config import STRIPE_SECRET_KEY

    if not STRIPE_SECRET_KEY:
        return None
    import stripe

    stripe.api_key = STRIPE_SECRET_KEY
    return stripe


def _sg(obj, key, default=None):
    """Stripe objects are NOT dicts (stripe 15.x): `.get` resolves through
    __getattr__ and raises. Index access with a default is the safe read."""
    try:
        value = obj[key]
    except (KeyError, TypeError):
        return default
    return default if value is None else value


def _epoch(ts: str) -> int:
    return int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp())


def _paid_charges(stripe, start: int, end: int | None = None):
    """Succeeded charges created in [start, end] (end open when None), as plain
    dicts. The one Stripe money read — Revenue and Ads both count from it."""
    created = {"gte": start} if end is None else {"gte": start, "lte": end}
    for ch in stripe.Charge.list(created=created, limit=100).auto_paging_iter():
        if not _sg(ch, "paid", False) or _sg(ch, "status") != "succeeded":
            continue
        yield {
            "customer": _sg(ch, "customer"),
            "amount": _sg(ch, "amount", 0),
            "refunded": _sg(ch, "amount_refunded", 0),
            "created": _sg(ch, "created", 0),
        }


def _section_revenue(from_ts: str, to_ts: str) -> dict:
    """Money actually taken, read from Stripe. Separate from the affiliate
    ledger on purpose: that one is a liability, this one is revenue."""
    stripe = _stripe_client()
    if stripe is None:
        return {
            "key": "revenue",
            "title": "Revenue",
            "unavailable_reason": "STRIPE_SECRET_KEY is not set on this backend.",
            "metrics": [],
            "timeseries": None,
            "tables": [],
        }

    g = _sg

    gross = refunded = 0
    by_date: dict[str, int] = defaultdict(int)
    for ch in _paid_charges(stripe, _epoch(from_ts), _epoch(to_ts)):
        amount = ch["amount"]
        back = ch["refunded"]
        gross += amount
        refunded += back
        day = datetime.fromtimestamp(ch["created"], UTC).date().isoformat()
        by_date[day] += amount - back

    # Normalise every active subscription to a monthly figure so weekly and
    # monthly plans can be added up without pretending a week is a month.
    mrr = 0
    active = 0
    plans: dict[str, int] = defaultdict(int)
    subs = stripe.Subscription.list(status="active", limit=100)
    for sub in subs.auto_paging_iter():
        active += 1
        for item in g(g(sub, "items", {}), "data", []):
            price = g(item, "price", {})
            unit = g(price, "unit_amount", 0)
            amount = unit * g(item, "quantity", 1)
            rec = g(price, "recurring", {})
            interval = g(rec, "interval")
            count = g(rec, "interval_count", 1) or 1
            if interval == "week":
                amount = round(amount * 52 / 12 / count)
            elif interval == "year":
                amount = round(amount / 12 / count)
            elif interval == "day":
                amount = round(amount * 365 / 12 / count)
            else:
                amount = round(amount / count)
            mrr += amount
            plans[g(price, "nickname") or f"{interval or '?'} ${unit / 100:.0f}"] += 1

    return {
        "key": "revenue",
        "title": "Revenue",
        "subtitle": "Money taken through Stripe, net of refunds.",
        "metrics": [
            _metric(
                "net_revenue",
                "Net revenue",
                _usd(gross - refunded),
                "currency",
                emphasis=True,
                description="Charges succeeded in this period minus what was refunded on them.",
            ),
            _metric(
                "mrr",
                "MRR",
                _usd(mrr),
                "currency",
                scope="current",
                emphasis=True,
                description="Active subscriptions normalised to a monthly amount (weekly x 52/12).",
            ),
            _metric("active_subscriptions", "Active subscriptions", active, scope="current"),
            _metric("refunded", "Refunded", _usd(refunded), "currency"),
        ],
        "timeseries": {
            "label": "Net revenue per day",
            "format": "currency",
            "points": [{"date": d, "value": _usd(v)} for d, v in sorted(by_date.items())],
        },
        "tables": [
            _table(
                "plans",
                "Active subscriptions by plan",
                [_col("plan", "Plan"), _col("count", "Subscriptions", "number", "right")],
                [{"plan": p, "count": c} for p, c in sorted(plans.items(), key=lambda kv: -kv[1])],
            )
        ],
    }


# --- endpoint ----------------------------------------------------------------


@router.get("/metrics")
def metrics(
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
    from_: str | None = Query(default=None, alias="from", description="YYYY-MM-DD, inclusive"),
    to: str | None = Query(default=None, description="YYYY-MM-DD, inclusive"),
) -> dict:
    """Every number the admin panel shows, in one call.

    Sections are built independently: a section whose query fails comes back
    with `unavailable_reason` set and empty metrics, so one broken table can
    never blank the board (and never silently shows a zero instead).
    """
    _require_admin(x_admin_token)

    today = datetime.now(UTC).date()
    try:
        to_day = date.fromisoformat(to) if to else today
        from_day = date.fromisoformat(from_) if from_ else to_day - timedelta(days=29)
    except ValueError as err:
        raise HTTPException(status_code=400, detail="from/to must be YYYY-MM-DD") from err
    if from_day > to_day:
        raise HTTPException(status_code=400, detail="from must not be after to")

    from_ts = f"{from_day.isoformat()}T00:00:00Z"
    to_ts = f"{to_day.isoformat()}T23:59:59Z"

    # Shared reads. Applications and profiles are each read ONCE, all-time, and
    # sliced per section in Python: three sections need overlapping windows of
    # the same rows, and at this scale one read beats three.
    # Paged: PostgREST silently stops at 1000 rows, and these are all-time reads.
    apps = _paged("applications", "user_id, date_applied, status, platform", ("date_applied",))
    apps.sort(key=lambda a: a.get("date_applied") or "", reverse=True)
    profiles = _paged(
        "profiles",
        "user_id, name, created_at, onboarding_completed, subscription_tier, "
        "subscription_expires_at, attribution, stripe_customer_id",
        ("created_at",),
        limit=PROFILE_LIMIT,
    )

    builders = [
        ("ops", lambda: _section_ops(apps, from_ts, to_ts)),
        ("revenue", lambda: _section_revenue(from_ts, to_ts)),
        ("users", lambda: _section_users(profiles, apps, from_ts, to_ts)),
        ("funnel", lambda: _section_funnel(profiles, apps, from_ts, to_ts)),
        (
            "ads",
            lambda: _section_ads(
                profiles, apps, from_ts, to_ts, from_day.isoformat(), to_day.isoformat()
            ),
        ),
        ("affiliates", lambda: _section_affiliates(from_ts, to_ts)),
        ("buddy", lambda: _section_buddy(from_ts, to_ts)),
        (
            "ai_cost",
            lambda: _section_ai_cost(
                apps, from_ts, to_ts, from_day.isoformat(), to_day.isoformat()
            ),
        ),
    ]

    sections = []
    for key, build in builders:
        try:
            section = build()
            section.setdefault("unavailable_reason", None)
            sections.append(section)
        except Exception as exc:  # noqa: BLE001
            # Named failure beats a silent zero: the panel renders the reason.
            sections.append(
                {
                    "key": key,
                    "title": key.replace("_", " ").title(),
                    "unavailable_reason": f"{type(exc).__name__}: {exc}"[:300],
                    "metrics": [],
                    "timeseries": None,
                    "tables": [],
                }
            )

    notes = []
    if len(apps) >= ROW_LIMIT:
        notes.append(f"applications read hit the {ROW_LIMIT}-row ceiling — counts under-report.")
    if len(profiles) >= PROFILE_LIMIT:
        notes.append(f"profiles read hit the {PROFILE_LIMIT}-row ceiling — counts under-report.")

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "period": {"from": from_day.isoformat(), "to": to_day.isoformat()},
        "notes": notes,
        "sections": sections,
    }
