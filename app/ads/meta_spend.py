"""Meta ad spend — pull daily per-ad Insights into `ad_spend`.

Env: META_ADS_TOKEN (system-user token with ads_read), META_AD_ACCOUNT_ID
(digits, with or without the `act_` prefix), optional META_GRAPH_VERSION.

Two callers: the admin board's Ads section (`sync_if_stale`, last 7 days when
the newest Meta row is older than an hour) and `scripts/ads_spend.py sync-meta`.
A failed sync never breaks either: the board shows the failure by name.

Second writer, the one live since 10-05: the token lost its rights through the
HelloMetrics app (dev mode, tied to the clinic — not touched), so the ads-manager
agent reads spend through the official Meta Ads MCP and hands the answer to
`scripts/ads_spend.py ingest-mcp` (`mcp_to_rows` below). Once the newest Meta row
is the agent's, the board stops calling Graph with the dead token.
"""

import json
import os
import sys
import threading
import time
from datetime import UTC, date, datetime, timedelta

import httpx

from app.ads.meta_capi import DEFAULT_GRAPH_VERSION, GRAPH_URL
from app.db import ad_spend as spend_db

# inline_link_clicks, not clicks: Meta's `clicks` counts every tap on the ad (likes,
# "see more", profile) and would flatter the CTR the kill rule reads. Link clicks are
# the ones that can land on hiredrop.io — the same meaning Google's clicks have.
FIELDS = (
    "campaign_id,campaign_name,adset_id,ad_id,ad_name,spend,impressions,"
    "inline_link_clicks,account_currency"
)
PAGE_LIMIT = 500
MAX_PAGES = 50
HTTP_TIMEOUT_SECONDS = 20.0

STALE_AFTER_MINUTES = 60
SYNC_DAYS = 7

MCP_SOURCE = "meta_mcp"
# The agent writes once a day; a missed run shows after a day plus slack.
MCP_STALE_AFTER_HOURS = 26

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


def to_rows(insights: list[dict], account_id: str, source: str = "meta_insights") -> list[dict]:
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
                "clicks": _int(r.get("inline_link_clicks")),
                "source": source,
            }
        )
    return rows


def _mcp_money(value) -> float | None:
    """MCP `amount_spent` → dollars. Accepts 12.3, "12.30", "$1,012.30" or
    {"amount"|"value": …, "currency": …}; None when absent. Raises on anything
    else, so an unknown shape is a loud failure, never a stored $0."""
    if value is None or value == "":
        return None
    if isinstance(value, dict):
        currency = str(value.get("currency") or "USD").upper()
        if currency != "USD":
            raise MetaSpendError(f"Meta MCP amount is in {currency}, spend_usd expects USD")
        return _mcp_money(value.get("amount", value.get("value")))
    if isinstance(value, bool):
        raise MetaSpendError(f"Meta MCP amount_spent is not money: {value!r}")
    if isinstance(value, int | float):
        return float(value)
    text = str(value).strip().replace(",", "").removeprefix("$").removeprefix("USD").strip()
    try:
        return float(text)
    except ValueError as exc:
        raise MetaSpendError(f"Meta MCP amount_spent is not money: {value!r}") from exc


def mcp_entities(payload) -> list[dict]:
    """The ad list out of a saved `ads_get_ad_entities` answer: the whole tool
    result (`{"ad_entities": "[…]"}` — the list arrives JSON-encoded inside JSON),
    its decoded list, or the raw string. A paged answer is refused: storing page
    one would undercount the day silently."""
    if isinstance(payload, str):
        payload = json.loads(payload)
    if isinstance(payload, dict):
        cursor = payload.get("next_cursor") or (payload.get("pagination") or {}).get("next_cursor")
        if cursor:
            raise MetaSpendError("Meta MCP answer is paged (next_cursor) — fetch every page")
        if "ad_entities" not in payload:
            raise MetaSpendError("Meta MCP answer has no ad_entities")
        payload = payload["ad_entities"]
        if isinstance(payload, str):
            payload = json.loads(payload)
    if not isinstance(payload, list):
        raise MetaSpendError("Meta MCP ad_entities is not a list")
    return payload


def mcp_to_rows(entities: list[dict], day: str, account_id: str) -> list[dict]:
    """Ad-level MCP entities for ONE day → ad_spend rows, via `to_rows` so both
    Meta writers store the same shape. The day comes from the request (the agent
    asks time_range since=until=day), not from the answer. An ad with no metrics
    at all did not deliver that day and gets no row — the same as Insights."""
    insights = []
    for e in entities:
        metrics = (e.get("amount_spent"), e.get("impressions"), e.get("link_click"))
        if all(m is None for m in metrics):
            continue
        spend = _mcp_money(e.get("amount_spent"))
        if spend is None:
            raise MetaSpendError(f"Meta MCP ad {e.get('id')} has metrics but no amount_spent")
        insights.append(
            {
                "date_start": day,
                "campaign_id": e.get("campaign_id"),
                "campaign_name": e.get("campaign_name"),
                "adset_id": e.get("adset_id"),
                "ad_id": e.get("id"),
                "ad_name": e.get("name"),
                "spend": spend,
                "impressions": e.get("impressions"),
                # Meta's link_click action = clicks to the destination, the meaning
                # inline_link_clicks has on the token path.
                "inline_link_clicks": e.get("link_click", e.get("actions:link_click")),
            }
        )
    return to_rows(insights, account_id, source=MCP_SOURCE)


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


def _agent_state(newest: dict | None) -> dict | None:
    """When the newest Meta row came from the ads-manager agent, the board's
    whole Meta state: fresh within a day, or a named miss. None otherwise."""
    if not newest or newest.get("source") != MCP_SOURCE:
        return None
    age = _age_minutes(newest.get("synced_at"))
    stale = age is None or age > MCP_STALE_AFTER_HOURS * 60
    return {
        "connected": True,
        "ran": False,
        "rows": None,
        "via": "mcp",
        "newest_synced_at": newest.get("synced_at"),
        "error": (
            "Meta spend is written by the ads-manager agent (Meta MCP); last write "
            + (f"{age / 60:.0f} h ago" if age is not None else "at an unreadable time")
            + " — no delivery since, or the daily run missed"
            if stale
            else None
        ),
    }


def sync_if_stale() -> dict:
    """Refresh the last SYNC_DAYS days if Meta data is older than an hour.

    Returns {"connected": bool, "ran": bool, "rows": int | None,
    "error": str | None, "newest_synced_at": str | None, "via": "token" | "mcp"}.
    Never raises. Rows written by the agent (Meta MCP) win over the token: the
    token is not called at all while the agent is the newest writer.
    """
    state = {"connected": False, "ran": False, "rows": None, "error": None, "via": "token"}
    try:
        newest = spend_db.newest_sync("meta")
    except Exception as exc:  # noqa: BLE001
        state["error"] = f"ad_spend unreadable: {type(exc).__name__}: {exc}"[:300]
        state["newest_synced_at"] = None
        return state
    agent = _agent_state(newest)
    if agent:
        return agent
    state["newest_synced_at"] = (newest or {}).get("synced_at")
    if not config():
        state["error"] = NOT_CONNECTED
        return state
    state["connected"] = True
    age = _age_minutes(state["newest_synced_at"])
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
