"""AI spend per application — the one computation the admin board and the daily report
(scripts/ai_cost_report.py) share, so the two can never disagree about what an
application costs or when to raise the alarm.

Input is the ledger's per-day sums (app.db.ai_calls.daily) and the applications rows; the
functions here are pure, so every number they produce is testable without a database.
"""

from collections import defaultdict
from datetime import date, datetime, timedelta
from statistics import median

from app.billing_config import PLANS
from app.db.subscriptions import TIER_LIMITS

# Default affiliate share, for the margin and ceiling lines only. The authoritative
# per-partner rate lives in affiliates.commission_pct.
AFFILIATE_RATE = 0.30

# The most AI may cost per application before the heaviest paying user loses us money:
# the monthly price after the affiliate share, spread over every application the paid cap
# allows in a month.
CEILING_PER_APPLICATION_USD = round(
    PLANS["monthly"]["price_usd"] * (1 - AFFILIATE_RATE) / (TIER_LIMITS["pro"] * 30), 4
)

# A day costs this many times the median of the week before it — loud enough to be a
# change, not noise.
JUMP_FACTOR = 1.5
# Spend on an account with no application in the window, above this, is named.
IDLE_SPEND_USD = 0.25
# Above this share of spend charged to nobody, a thread or pool is missing its binding.
UNATTRIBUTED_SHARE = 0.20


def _days(from_day: str, to_day: str) -> list[str]:
    start, end = date.fromisoformat(from_day), date.fromisoformat(to_day)
    return [(start + timedelta(days=i)).isoformat() for i in range((end - start).days + 1)]


def _per(cost: float, n: int) -> float | None:
    return round(cost / n, 4) if n else None


def _before(stamp: str, since: str) -> bool:
    try:
        return datetime.fromisoformat(stamp) < datetime.fromisoformat(since)
    except ValueError:
        return stamp < since


def summarize(
    daily: list[dict],
    applications: list[dict],
    from_day: str,
    to_day: str,
    metered_since: str | None = None,
) -> dict:
    """Spend, calls and applications per UTC day, account, purpose and model.

    `daily`: ai_calls_daily rows. `applications`: rows with user_id and date_applied; rows
    outside [from_day, to_day] are ignored, so the caller may pass a wider read.
    `metered_since`: when the ledger's first call was recorded. An application before it
    had its AI spend go unrecorded, so counting it would divide real spend by applications
    that cost "nothing" and show a price several times too low.
    """
    days = _days(from_day, to_day)
    in_range = set(days)
    cost_day: dict[str, float] = defaultdict(float)
    calls_day: dict[str, int] = defaultdict(int)
    apps_day: dict[str, int] = defaultdict(int)
    by_account: dict[str | None, dict] = defaultdict(
        lambda: {"cost": 0.0, "calls": 0, "applications": 0}
    )
    by_purpose: dict[str, dict] = defaultdict(lambda: {"cost": 0.0, "calls": 0})
    by_model: dict[str, dict] = defaultdict(lambda: {"cost": 0.0, "calls": 0})
    unpriced = 0
    tokens = {"input": 0, "cache_read": 0, "cache_write": 0}

    for row in daily:
        day = str(row.get("day") or "")[:10]
        if day not in in_range:
            continue
        cost = float(row.get("cost_usd") or 0)
        calls = int(row.get("calls") or 0)
        cost_day[day] += cost
        calls_day[day] += calls
        for bucket in (
            by_account[row.get("user_id")],
            by_purpose[row.get("purpose") or "?"],
            by_model[row.get("model") or "?"],
        ):
            bucket["cost"] += cost
            bucket["calls"] += calls
        unpriced += int(row.get("unpriced_calls") or 0)
        tokens["input"] += int(row.get("input_tokens") or 0)
        tokens["cache_read"] += int(row.get("cache_read_tokens") or 0)
        tokens["cache_write"] += int(row.get("cache_write_tokens") or 0)

    for app in applications:
        stamp = app.get("date_applied") or ""
        day = stamp[:10]
        if day not in in_range or (metered_since and _before(stamp, metered_since)):
            continue
        apps_day[day] += 1
        by_account[app.get("user_id")]["applications"] += 1

    total_cost = sum(cost_day.values())
    total_apps = sum(apps_day.values())
    prompt_tokens = sum(tokens.values())

    def ranked(groups: dict, key: str) -> list[dict]:
        rows = [
            {key: k, **{f: round(v, 6) if f == "cost" else v for f, v in g.items()}}
            for k, g in groups.items()
        ]
        return sorted(rows, key=lambda r: -r["cost"])

    accounts = ranked(by_account, "user_id")
    for a in accounts:
        a["per_application"] = _per(a["cost"], a["applications"])

    return {
        "from_day": from_day,
        "to_day": to_day,
        "days": [
            {
                "day": d,
                "cost": round(cost_day[d], 6),
                "calls": calls_day[d],
                "applications": apps_day[d],
                "per_application": _per(cost_day[d], apps_day[d]),
                "metered": not metered_since or d >= metered_since[:10],
            }
            for d in days
        ],
        "cost": round(total_cost, 6),
        "calls": sum(calls_day.values()),
        "applications": total_apps,
        "per_application": _per(total_cost, total_apps),
        "calls_per_application": round(sum(calls_day.values()) / total_apps, 2)
        if total_apps
        else None,
        "by_account": accounts,
        "by_purpose": ranked(by_purpose, "purpose"),
        "by_model": ranked(by_model, "model"),
        "unattributed_cost": round(by_account[None]["cost"], 6) if None in by_account else 0.0,
        "unpriced_calls": unpriced,
        "cache_read_share": round(tokens["cache_read"] / prompt_tokens, 4)
        if prompt_tokens
        else None,
    }


def alerts(summary: dict, ceiling: float = CEILING_PER_APPLICATION_USD) -> list[str]:
    """What a person should look at, one line each. Empty = nothing to report.

    Judged on the last day of the summary (the report passes yesterday as its end): over
    the ceiling, a jump against the median of the days before it, spend on accounts with
    no application, spend nobody is charged with, and calls of a model with no price.
    """
    out: list[str] = []
    days = summary["days"]
    if days:
        last = days[-1]
        per = last["per_application"]
        if per is not None and per > ceiling:
            out.append(
                f"{last['day']}: AI per application ${per:.4f} is over the ${ceiling:.4f} "
                f"ceiling ({last['applications']} applications, ${last['cost']:.2f})"
            )
        if per is None and last["cost"] >= IDLE_SPEND_USD:
            out.append(f"{last['day']}: ${last['cost']:.2f} of AI with 0 applications")
        before = [d["per_application"] for d in days[-8:-1] if d["per_application"] is not None]
        if per is not None and before and per > JUMP_FACTOR * median(before):
            out.append(
                f"{last['day']}: AI per application ${per:.4f} jumped from a "
                f"${median(before):.4f} median of the days before"
            )
    for a in summary["by_account"]:
        if a["user_id"] is not None and not a["applications"] and a["cost"] >= IDLE_SPEND_USD:
            out.append(
                f"account {str(a['user_id'])[:8]}: ${a['cost']:.2f} of AI and 0 applications "
                f"in {summary['from_day']}..{summary['to_day']}"
            )
    if summary["cost"] and summary["unattributed_cost"] / summary["cost"] > UNATTRIBUTED_SHARE:
        share = summary["unattributed_cost"] / summary["cost"]
        out.append(
            f"{share:.0%} of AI spend is charged to no account — a thread or pool is "
            "missing ai_meter.attributed"
        )
    if summary["unpriced_calls"]:
        out.append(
            f"{summary['unpriced_calls']} calls of a model with no price — add it to "
            "ai_meter.PRICES"
        )
    return out
