"""Admin metrics — the whole HireDrop admin dashboard as ONE read-only endpoint.

Why it lives here and not in a dashboard repo: the counting must sit next to the
data. The panel that renders this (Hellometrix, `/hiredrop`) is a thin screen —
it fetches this JSON and draws it. That split means the dashboard never holds a
service_role key for this database, and changing a metric is a change here, in
the product repo, not in someone else's app.

SECURITY
  * Read-only. Every query below is a SELECT; this router must never write.
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
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query

from app.billing_config import PLANS
from app.db.client import get_supabase

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

# Measured 2026-09-15 (scripts/measure_ai_cost.py) WITH per-application resume
# tailoring on. Before tailoring the same measurement read $0.0129.
COST_PER_APPLICATION_USD = 0.0199
# Calls per application at the time of that measurement. Drift = re-measure.
MEASURED_CALLS_PER_APPLICATION = 3.5

# Default affiliate share, for the "what's left" line only. The authoritative
# per-partner rate lives in affiliates.commission_pct.
AFFILIATE_RATE = 0.30

# Row ceilings. At today's scale nothing comes close; a read that DID hit one
# would silently under-count, so each section says so in `notes`.
ROW_LIMIT = 20000
ROSTER_LIMIT = 50
SPENDER_LIMIT = 25
LOG_LIMIT = 5000


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
    a = attribution or {}
    if isinstance(a.get("ref"), str) and a["ref"]:
        return f"ref:{a['ref']}"
    if isinstance(a.get("utm_source"), str) and a["utm_source"]:
        return a["utm_source"]
    return "direct"


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
    text = re.sub(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", "<id>", text, flags=re.I)
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
    except Exception:  # noqa: BLE001
        pass  # activity_log unavailable — the rest of the section still renders

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
            [{"platform": p, "count": c} for p, c in _tally([a.get("platform") or "unknown" for a in period])],
        ),
        _table(
            "by_status",
            "Applications by status",
            [_col("status", "Status"), _col("count", "Count", "number", "right")],
            [{"status": s, "count": c} for s, c in _tally([a.get("status") or "unknown" for a in period])],
        ),
    ]
    if top_errors:
        tables.append(
            _table(
                "top_errors",
                "What broke most",
                [_col("message", "Error"), _col("phase", "Phase"), _col("count", "Times", "number", "right")],
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
            _metric("paid_now", "Paying now", len(paying), scope="current", emphasis=True,
                    description="Accounts on a paid tier that has not expired."),
            _metric("total_users", "Accounts", len(users), scope="all_time", emphasis=True),
            _metric("signups_period", "New signups", len(in_period)),
            _metric("active_7d", "Active (7d)", len(active7), scope="current",
                    description="Sent at least one application in the last 7 days."),
            _metric("onboarded_share", "Onboarded", _pct(len(onboarded), len(users)), "percent",
                    scope="all_time"),
            _metric("never_applied", "Never applied", len(never), scope="all_time",
                    description="Signed up and never sent one application — the real activation gap."),
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
                [{k: u[k] for k in ("name", "tier", "source", "signed_up", "last_apply", "onboarded", "applications")}
                 for u in roster],
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


def _section_funnel(profiles: list[dict], apps: list[dict], from_ts: str, to_ts: str) -> dict:
    now_iso = datetime.now(UTC).isoformat()
    # Period filters use each table's real timestamp column: profiles/
    # extension_keys -> created_at, campaign_states -> started_at,
    # applications -> date_applied.
    signups = [p for p in profiles if from_ts <= (p.get("created_at") or "") <= to_ts]
    onboarded = [p for p in signups if p.get("onboarding_completed") is True]
    connected = _count("extension_keys", created_at__gte=from_ts, created_at__lte=to_ts)
    campaigns = _count("campaign_states", started_at__gte=from_ts, started_at__lte=to_ts)
    submitted = len([a for a in apps if from_ts <= (a.get("date_applied") or "") <= to_ts])
    paid_now = _count("profiles", subscription_expires_at__gt=now_iso)

    steps = [
        {"step": "Signed up", "count": len(signups), "of_signups": 100.0 if signups else None},
        {"step": "Completed onboarding", "count": len(onboarded), "of_signups": _pct(len(onboarded), len(signups))},
        {"step": "Connected extension", "count": connected, "of_signups": _pct(connected, len(signups))},
        {"step": "Started a campaign", "count": campaigns, "of_signups": _pct(campaigns, len(signups))},
    ]

    return {
        "key": "funnel",
        "title": "Funnel",
        "subtitle": "Signup → onboarding → extension → campaign → applications.",
        "metrics": [
            _metric("signups", "Signups", len(signups), emphasis=True),
            _metric("campaigns_started", "Campaigns started", campaigns),
            _metric("applications", "Applications sent", submitted),
            _metric("paid_users", "Paid users", paid_now, scope="current", emphasis=True,
                    description="Profiles on a paid tier right now (not period-scoped)."),
            _metric("activation_rate", "Signup → campaign", _pct(campaigns, len(signups)), "percent",
                    description="Share of period signups that started their first campaign."),
            _metric("extension_rate", "Extension connect rate", _pct(connected, len(signups)), "percent"),
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
                [_col("step", "Step"), _col("count", "Count", "number", "right"),
                 _col("of_signups", "% of signups", "percent", "right")],
                steps,
            )
        ],
    }


def _section_ai_cost(apps: list[dict], from_ts: str, to_ts: str, from_day: str, to_day: str) -> dict:
    # cover_letter_usage is keyed by (user_id, date) with a plain DATE column —
    # one shared counter for cover letters, screener answers and resume work.
    usage = _rows("cover_letter_usage", "user_id, date, count", date__gte=from_day, date__lte=to_day)
    period = [a for a in apps if from_ts <= (a.get("date_applied") or "") <= to_ts]

    calls = sum(int(u.get("count") or 0) for u in usage)
    spend = len(period) * COST_PER_APPLICATION_USD
    calls_per_app = round(calls / len(period), 2) if period else None

    calls_by_user: dict[str, int] = defaultdict(int)
    for u in usage:
        calls_by_user[u.get("user_id") or "?"] += int(u.get("count") or 0)
    apps_by_user: dict[str, int] = defaultdict(int)
    for a in period:
        apps_by_user[a.get("user_id") or "?"] += 1

    spenders = []
    for uid in set(calls_by_user) | set(apps_by_user):
        n_apps = apps_by_user.get(uid, 0)
        n_calls = calls_by_user.get(uid, 0)
        spenders.append(
            {
                # A short id only: cost shape belongs here, names belong in the roster.
                "account": uid[:8],
                "applications": n_apps,
                "calls": n_calls,
                "per_app": round(n_calls / n_apps, 2) if n_apps else 0,
                "cost": round(n_apps * COST_PER_APPLICATION_USD, 2),
            }
        )
    spenders.sort(key=lambda s: (-s["applications"], -s["calls"]))

    metrics = [
        _metric("ai_spend", "AI spend (est.)", round(spend, 2), "currency", emphasis=True,
                description=f"Applications x ${COST_PER_APPLICATION_USD} — measured 2026-09-15, not a live token read."),
        _metric("ai_calls", "AI calls", calls, emphasis=True,
                description="Counted for real: every billable call through the shared daily quota."),
        _metric("applications", "Applications", len(period)),
        _metric("cost_per_application", "Cost / application", COST_PER_APPLICATION_USD, "currency",
                scope="all_time",
                description="Measured constant with resume tailoring on. Re-measure: scripts/measure_ai_cost.py."),
    ]
    if calls_per_app is not None:
        metrics.append(
            _metric("calls_per_application", "Calls / application", calls_per_app,
                    description=f"Measured at {MEASURED_CALLS_PER_APPLICATION} when the cost above was taken — "
                                "drift means the dollar figure is stale.")
        )
    metrics.append(_metric("active_spenders", "Accounts spending", len(calls_by_user)))

    if calls_by_user:
        monthly = PLANS["monthly"]["price_usd"]
        left = monthly * (1 - AFFILIATE_RATE) - spend / len(calls_by_user)
        metrics.append(
            _metric("margin_per_paying_user", f"Left after AI + {int(AFFILIATE_RATE * 100)}%",
                    round(left, 2), "currency",
                    description=f"${monthly} monthly minus the affiliate share minus this period's AI cost per active account.")
        )

    by_date: dict[str, int] = defaultdict(int)
    for u in usage:
        by_date[str(u.get("date"))] += int(u.get("count") or 0)

    return {
        "key": "ai_cost",
        "title": "AI cost",
        "subtitle": "Calls and applications are counted; dollars are derived from a measured constant.",
        "metrics": metrics,
        "timeseries": {
            "label": "AI calls per day",
            "format": "number",
            "points": [{"date": d, "value": v} for d, v in sorted(by_date.items())],
        },
        "tables": [
            _table(
                "spenders",
                "Cost by account",
                [_col("account", "Account"), _col("applications", "Apps", "number", "right"),
                 _col("calls", "Calls", "number", "right"), _col("per_app", "Calls/app", "number", "right"),
                 _col("cost", "Cost", "currency", "right")],
                spenders[:SPENDER_LIMIT],
            )
        ],
    }


def _section_affiliates(from_ts: str, to_ts: str) -> dict:
    payable_cutoff = (datetime.now(UTC) - timedelta(days=REFUND_WINDOW_DAYS)).isoformat()

    affiliates = _rows("affiliates", "id, code, status, commission_pct, paypal_email")
    referrals = _rows("referrals", "affiliate_id, first_seen_at, first_paid_at")
    # The whole ledger, not just the period: "owed" is an all-time liability.
    commissions = _rows("commissions", "affiliate_id, amount_cents, status, created_at")
    payouts = _rows("payouts", "affiliate_id, amount_cents, paid_at")
    # The inbox: people asking for a link. Approval is a human decision made
    # on this board (POST /admin/affiliates/decide), never automatic.
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

    earned_period = sum(c["amount_cents"] for c in live if in_period(c.get("created_at")))
    reversed_period = sum(
        c["amount_cents"] for c in commissions
        if c.get("status") == "reversed" and in_period(c.get("created_at"))
    )
    owed_total = sum(c["amount_cents"] for c in accrued)
    payable_now = sum(c["amount_cents"] for c in accrued if (c.get("created_at") or "") < payable_cutoff)
    paid_period = sum(p["amount_cents"] for p in payouts if in_period(p.get("paid_at")))

    rows = []
    for a in affiliates:
        mine = [r for r in referrals if r.get("affiliate_id") == a["id"]]
        cs = [c for c in commissions if c.get("affiliate_id") == a["id"]]
        mine_accrued = [c for c in cs if c.get("status") == "accrued"]
        rows.append(
            {
                "code": a.get("code"),
                "status": a.get("status"),
                "rate": f"{round(float(a.get('commission_pct') or 0))}%",
                "signups": len(mine),
                "paying": len([r for r in mine if r.get("first_paid_at")]),
                "earned": _usd(sum(c["amount_cents"] for c in cs if c.get("status") != "reversed")),
                "owed": _usd(sum(c["amount_cents"] for c in mine_accrued)),
                "payable": _usd(sum(c["amount_cents"] for c in mine_accrued
                                    if (c.get("created_at") or "") < payable_cutoff)),
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
            _metric("payable_now", "Ready to pay out", _usd(payable_now), "currency", scope="current",
                    emphasis=True,
                    description=f"Commission past the {REFUND_WINDOW_DAYS}-day refund window — payable today."),
            _metric("owed_total", "Owed (incl. holding)", _usd(owed_total), "currency", scope="all_time",
                    emphasis=True,
                    description="Every accrued, unpaid commission — including what is still inside the refund window."),
            _metric("earned_period", "Commission accrued", _usd(earned_period), "currency"),
            _metric("paid_period", "Paid out", _usd(paid_period), "currency",
                    description="Payouts recorded in this period (entered by hand after PayPal)."),
            _metric("active_affiliates", "Active partners",
                    len([a for a in affiliates if a.get("status") == "active"]), scope="current"),
            _metric("referred_signups", "Referred signups",
                    len([r for r in referrals if in_period(r.get("first_seen_at"))])),
            _metric("paying_referrals", "Referrals paying",
                    len([r for r in referrals if r.get("first_paid_at")]), scope="current"),
            _metric("reversed_period", "Clawed back", _usd(reversed_period), "currency",
                    description="Commission reversed by customer refunds in this period."),
            _metric("applications_waiting", "Applications waiting",
                    len([a for a in applications if a.get("status") == "new"]), scope="current",
                    emphasis=True,
                    description="People who asked for a link and are waiting on a decision."),
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
                [_col("name", "Name"), _col("email", "Email"), _col("desired_code", "Wants link"),
                 _col("audience", "Audience"), _col("audience_size", "Size"),
                 _col("promo_plan", "How they'll share"), _col("paypal", "PayPal"),
                 _col("source", "Came from"), _col("applied", "Applied", "date"),
                 _col("id", "id")],
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
                [_col("name", "Name"), _col("desired_code", "Link"), _col("status", "Decision"),
                 _col("linked", "Live"), _col("applied", "Applied", "date")],
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
                        key=lambda a: a.get("created_at") or "", reverse=True,
                    )
                ],
            ),
            _table(
                "partners",
                "Partner ledger",
                [_col("code", "Code"), _col("status", "Status"), _col("rate", "Rate"),
                 _col("signups", "Signups", "number", "right"), _col("paying", "Paying", "number", "right"),
                 _col("earned", "Earned", "currency", "right"), _col("owed", "Owed", "currency", "right"),
                 _col("payable", "Payable", "currency", "right"), _col("paypal", "PayPal")],
                rows,
            )
        ],
    }


def _section_revenue(from_ts: str, to_ts: str) -> dict:
    """Money actually taken, read from Stripe. Separate from the affiliate
    ledger on purpose: that one is a liability, this one is revenue."""
    from config import STRIPE_SECRET_KEY

    if not STRIPE_SECRET_KEY:
        return {
            "key": "revenue",
            "title": "Revenue",
            "unavailable_reason": "STRIPE_SECRET_KEY is not set on this backend.",
            "metrics": [],
            "timeseries": None,
            "tables": [],
        }

    import stripe

    stripe.api_key = STRIPE_SECRET_KEY

    def g(obj, key, default=None):
        """Stripe objects are NOT dicts (stripe 15.x): `.get` resolves through
        __getattr__ and raises. Index access with a default is the safe read."""
        try:
            value = obj[key]
        except (KeyError, TypeError):
            return default
        return default if value is None else value

    start = int(datetime.fromisoformat(from_ts.replace("Z", "+00:00")).timestamp())
    end = int(datetime.fromisoformat(to_ts.replace("Z", "+00:00")).timestamp())

    gross = refunded = 0
    by_date: dict[str, int] = defaultdict(int)
    charges = stripe.Charge.list(created={"gte": start, "lte": end}, limit=100)
    for ch in charges.auto_paging_iter():
        if not g(ch, "paid", False) or g(ch, "status") != "succeeded":
            continue
        amount = g(ch, "amount", 0)
        back = g(ch, "amount_refunded", 0)
        gross += amount
        refunded += back
        day = datetime.fromtimestamp(g(ch, "created", 0), UTC).date().isoformat()
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
            _metric("net_revenue", "Net revenue", _usd(gross - refunded), "currency", emphasis=True,
                    description="Charges succeeded in this period minus what was refunded on them."),
            _metric("mrr", "MRR", _usd(mrr), "currency", scope="current", emphasis=True,
                    description="Active subscriptions normalised to a monthly amount (weekly x 52/12)."),
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
    apps = _rows(
        "applications", "user_id, date_applied, status, platform"
    )
    apps.sort(key=lambda a: a.get("date_applied") or "", reverse=True)
    profiles = _rows(
        "profiles",
        "user_id, name, created_at, onboarding_completed, subscription_tier, "
        "subscription_expires_at, attribution",
        limit=5000,
    )

    builders = [
        ("ops", lambda: _section_ops(apps, from_ts, to_ts)),
        ("revenue", lambda: _section_revenue(from_ts, to_ts)),
        ("users", lambda: _section_users(profiles, apps, from_ts, to_ts)),
        ("funnel", lambda: _section_funnel(profiles, apps, from_ts, to_ts)),
        ("affiliates", lambda: _section_affiliates(from_ts, to_ts)),
        ("ai_cost", lambda: _section_ai_cost(apps, from_ts, to_ts, from_day.isoformat(), to_day.isoformat())),
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
    if len(profiles) >= 5000:
        notes.append("profiles read hit the 5000-row ceiling — counts under-report.")

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "period": {"from": from_day.isoformat(), "to": to_day.isoformat()},
        "notes": notes,
        "sections": sections,
    }
