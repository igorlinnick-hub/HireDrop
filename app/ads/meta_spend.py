"""Meta ad spend — pull daily per-ad Insights into `ad_spend`.

Env: META_ADS_TOKEN (system-user token with ads_read), META_AD_ACCOUNT_ID
(digits, with or without the `act_` prefix), optional META_GRAPH_VERSION.

Two callers: the admin board's Ads section (`sync_if_stale`, last 7 days when
the newest Meta row is older than an hour) and `scripts/ads_spend.py sync-meta`.
A failed sync never breaks either: the board shows the failure by name.
"""

import os
import sys
import threading
import time
from datetime import UTC, date, datetime, timedelta

import httpx

from app.ads.meta_capi import DEFAULT_GRAPH_VERSION, GRAPH_URL
from app.db import ad_spend as spend_db

FIELDS = (
    "campaign_id,campaign_name,adset_id,ad_id,ad_name,spend,impressions,clicks,account_currency"
)
PAGE_LIMIT = 500
MAX_PAGES = 50
HTTP_TIMEOUT_SECONDS = 20.0

STALE_AFTER_MINUTES = 60
SYNC_DAYS = 7

NOT_CONNECTED = "Meta not connected: set META_ADS_TOKEN + META_AD_ACCOUNT_ID"

# In-process memo of the last attempt: a sync that returns no rows (nothing
# delivered yet) leaves synced_at untouched, and without this every board load
# would call Meta again. Reset on deploy, which is fine.
_last_attempt: dict = {"at": 0.0, "error": None}
_lock = threading.Lock()


class MetaSpendError(RuntimeError):
    """Meta answered, but not with data we can store."""


def config() -> dict | None:
    token = os.getenv("META_ADS_TOKEN", "").strip()
    account = os.getenv("META_AD_ACCOUNT_ID", "").strip()
    if not token or not account:
        return None
    return {
        "token": token,
        "account_id": account.removeprefix("act_"),
        "version": os.getenv("META_GRAPH_VERSION", "").strip() or DEFAULT_GRAPH_VERSION,
    }


def _int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def to_rows(insights: list[dict], account_id: str) -> list[dict]:
    """Insights rows → ad_spend rows. Pure; raises on a non-USD account because
    `spend_usd` would otherwise silently hold euros."""
    rows = []
    for r in insights:
        currency = (r.get("account_currency") or "USD").upper()
        if currency != "USD":
            raise MetaSpendError(f"Meta ad account currency is {currency}, spend_usd expects USD")
        if not r.get("ad_id") or not r.get("date_start"):
            continue
        rows.append(
            {
                "date": r["date_start"],
                "platform": "meta",
                "account_id": account_id,
                "campaign_id": r.get("campaign_id"),
                "campaign_name": r.get("campaign_name"),
                "adset_id": r.get("adset_id"),
                "ad_id": str(r["ad_id"]),
                "ad_name": r.get("ad_name"),
                "spend_usd": round(float(r.get("spend") or 0), 2),
                "impressions": _int(r.get("impressions")),
                "clicks": _int(r.get("clicks")),
                "source": "meta_insights",
            }
        )
    return rows


def fetch_insights(cfg: dict, since: date, until: date) -> list[dict]:
    url = f"{GRAPH_URL}/{cfg['version']}/act_{cfg['account_id']}/insights"
    params: dict | None = {
        "access_token": cfg["token"],
        "level": "ad",
        "time_increment": 1,
        "fields": FIELDS,
        "time_range": f'{{"since":"{since.isoformat()}","until":"{until.isoformat()}"}}',
        "limit": PAGE_LIMIT,
    }
    out: list[dict] = []
    for _ in range(MAX_PAGES):
        res = httpx.get(url, params=params, timeout=HTTP_TIMEOUT_SECONDS)
        if res.status_code != 200:
            # Meta's error body names the problem (expired token, missing
            # ads_read, wrong account id) — carry its message, never the URL.
            try:
                message = res.json().get("error", {}).get("message") or res.text
            except ValueError:
                message = res.text
            raise MetaSpendError(f"Meta Insights HTTP {res.status_code}: {str(message)[:200]}")
        body = res.json()
        out.extend(body.get("data") or [])
        next_url = (body.get("paging") or {}).get("next")
        if not next_url:
            return out
        # `next` is a complete URL that already carries the token and cursor.
        url, params = next_url, None
    raise MetaSpendError(f"Meta Insights paging did not end after {MAX_PAGES} pages")


def sync(since: date, until: date) -> int:
    """Pull [since, until] and upsert. Returns rows written. Raises on failure."""
    cfg = config()
    if not cfg:
        raise MetaSpendError(NOT_CONNECTED)
    rows = to_rows(fetch_insights(cfg, since, until), cfg["account_id"])
    return spend_db.upsert(rows) if rows else 0


def _age_minutes(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        then = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (datetime.now(UTC) - then).total_seconds() / 60


def sync_if_stale() -> dict:
    """Refresh the last SYNC_DAYS days if Meta data is older than an hour.

    Returns {"connected": bool, "ran": bool, "rows": int | None,
    "error": str | None, "newest_synced_at": str | None}. Never raises.
    """
    state = {"connected": False, "ran": False, "rows": None, "error": None}
    if not config():
        state["error"] = NOT_CONNECTED
        state["newest_synced_at"] = None
        return state
    state["connected"] = True
    try:
        newest = spend_db.newest_synced_at("meta")
    except Exception as exc:  # noqa: BLE001
        state["error"] = f"ad_spend unreadable: {type(exc).__name__}: {exc}"[:300]
        state["newest_synced_at"] = None
        return state
    state["newest_synced_at"] = newest
    age = _age_minutes(newest)
    fresh_rows = age is not None and age < STALE_AFTER_MINUTES
    with _lock:
        recently_tried = time.monotonic() - _last_attempt["at"] < STALE_AFTER_MINUTES * 60
        if fresh_rows or (_last_attempt["at"] and recently_tried):
            state["error"] = None if fresh_rows else _last_attempt["error"]
            return state
        _last_attempt["at"] = time.monotonic()
    today = datetime.now(UTC).date()
    try:
        state["rows"] = sync(today - timedelta(days=SYNC_DAYS - 1), today)
        state["ran"] = True
        _last_attempt["error"] = None
    except Exception as exc:  # noqa: BLE001
        message = f"Meta spend sync failed: {type(exc).__name__}: {exc}"[:300]
        print(f"[meta_spend] {message}", file=sys.stderr)
        state["error"] = message
        _last_attempt["error"] = message
    return state
