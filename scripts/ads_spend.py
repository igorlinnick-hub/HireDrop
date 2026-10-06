#!/usr/bin/env python3
"""Ad spend — what's connected, pull Meta now, or record spend by hand.

The admin board's Ads tab reads `ad_spend`. Four writers fill it: the Meta
Insights sync (automatic from the board, or `sync-meta` here — its token is dead
since 10-05), `ingest-mcp` here (the ads-manager agent's Meta MCP answer, the
live Meta writer), the Google Ads Script (scripts/google_ads_spend.js → POST
/admin/ads/spend), and `add` here for any channel with no API (a newsletter
slot, Reddit, a campus flyer run).

USAGE (from jobflow/, service_role key in .env)
  .venv/bin/python scripts/ads_spend.py status
  .venv/bin/python scripts/ads_spend.py sync-meta --days 7
  .venv/bin/python scripts/ads_spend.py ingest-mcp --date 2026-10-06 answer.json
  .venv/bin/python scripts/ads_spend.py add --platform manual --channel reddit \\
      --date 2026-09-28 --campaign "r/cscareerquestions promo" --spend 25

`add --channel` is the utm_source that spend bought: the board adds that
spend to the signups arriving with the same utm_source. Re-running `add` for
the same date + campaign overwrites, it does not double.

`ingest-mcp` takes ONE day: the saved result of `ads_get_ad_entities` with
level=ad, time_range since=until=that date, fields id, name, campaign_id,
campaign_name, adset_id, amount_spent, impressions, link_click (no
time_increment — the date comes from --date). Re-running a day overwrites.
`--dry-run` prints the rows without writing.
"""

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402,F401 — importing it loads .env
from app.ads import meta_capi, meta_spend  # noqa: E402
from app.db import ad_spend as spend_db  # noqa: E402


def _yes(flag: bool) -> str:
    return "yes" if flag else "no"


def cmd_status(_args) -> int:
    print("Connections (env on THIS machine — Railway has its own):")
    print(f"  Meta spend   META_ADS_TOKEN + META_AD_ACCOUNT_ID : {_yes(bool(meta_spend.config()))}")
    print(f"  Meta CAPI    META_PIXEL_ID + META_CAPI_TOKEN     : {_yes(bool(meta_capi.config()))}")
    print(
        "  Google       ADS_INGEST_TOKEN                    : "
        f"{_yes(bool(os.getenv('ADS_INGEST_TOKEN')))}"
    )
    try:
        rows = spend_db.read_all()
    except Exception as exc:  # noqa: BLE001
        print(f"\nad_spend unreadable: {getattr(exc, 'message', None) or exc}")
        print("Is migrations/2026-09-28_ad_spend.sql applied?")
        return 1

    since = (datetime.now(UTC).date() - timedelta(days=29)).isoformat()
    by_platform: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_platform[r["platform"]].append(r)
    print(
        f"\n{'platform':9} {'rows':>6} {'first':>11} {'last':>11} {'spend 30d':>10}  last sync (UTC) (writer)"
    )
    for platform in spend_db.PLATFORMS:
        mine = by_platform.get(platform, [])
        if not mine:
            print(f"{platform:9} {0:>6} {'—':>11} {'—':>11} {'—':>10}  never")
            continue
        dates = sorted(str(r["date"]) for r in mine)
        spend30 = sum(float(r.get("spend_usd") or 0) for r in mine if str(r["date"]) >= since)
        last = max(mine, key=lambda r: str(r.get("synced_at") or ""))
        synced = str(last.get("synced_at") or "")[:16].replace("T", " ")
        print(
            f"{platform:9} {len(mine):>6} {dates[0]:>11} {dates[-1]:>11} "
            f"{'$' + format(spend30, ',.2f'):>10}  {synced} ({last.get('source') or '?'})"
        )
    return 0


def cmd_sync_meta(args) -> int:
    if not meta_spend.config():
        print(meta_spend.NOT_CONNECTED)
        return 1
    until = datetime.now(UTC).date()
    since = until - timedelta(days=max(1, args.days) - 1)
    try:
        written = meta_spend.sync(since, until)
    except Exception as exc:  # noqa: BLE001
        print(f"Meta sync failed: {exc}")
        return 1
    print(f"Meta {since} → {until}: {written} rows upserted.")
    return 0


def cmd_ingest_mcp(args) -> int:
    account = (os.getenv("META_AD_ACCOUNT_ID", "") or args.account).strip().removeprefix("act_")
    if args.date > datetime.now(UTC).date():
        print(f"--date {args.date} is in the future")
        return 2
    try:
        entities = []
        for path in args.files:
            entities.extend(meta_spend.mcp_entities(json.loads(Path(path).read_text())))
        rows = meta_spend.mcp_to_rows(entities, args.date.isoformat(), account)
    except (OSError, ValueError, meta_spend.MetaSpendError) as exc:
        print(f"Meta MCP ingest refused: {exc}")
        return 1
    total = sum(r["spend_usd"] for r in rows)
    for r in rows:
        print(
            f"  {r['date']} ad {r['ad_id']} {r.get('ad_name') or ''!s:30.30} "
            f"${r['spend_usd']:.2f} · {r.get('impressions') or 0} impr · {r.get('clicks') or 0} clicks"
        )
    if args.dry_run:
        print(f"Dry run: {len(rows)} rows, ${total:.2f} for {args.date} — nothing written.")
        return 0
    written = spend_db.upsert(rows) if rows else 0
    print(
        f"Meta MCP {args.date}: {written} rows upserted, ${total:.2f}"
        + ("" if rows else " (no ad delivered that day)")
        + "."
    )
    return 0


def cmd_add(args) -> int:
    campaign = args.campaign.strip()
    if not campaign:
        print("--campaign is required")
        return 2
    channel = (args.channel or "").strip().lower() or None
    row = {
        "date": args.date.isoformat(),
        "platform": args.platform,
        "account_id": channel,
        "campaign_id": None,
        "campaign_name": campaign,
        "ad_id": args.ad or f"manual:{campaign.lower()}",
        "ad_name": args.ad,
        "spend_usd": round(args.spend, 2),
        "impressions": args.impressions,
        "clicks": args.clicks,
        "source": "cli",
    }
    spend_db.upsert([row])
    print(
        f"Recorded ${row['spend_usd']:.2f} on {row['date']} for '{campaign}' (channel {channel or 'manual'})."
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="what is connected, rows and last sync per platform")

    p = sub.add_parser("sync-meta", help="pull Meta Insights now")
    p.add_argument("--days", type=int, default=7)

    p = sub.add_parser("ingest-mcp", help="store one day of the agent's Meta MCP answer")
    p.add_argument("--date", type=date.fromisoformat, required=True, help="YYYY-MM-DD")
    p.add_argument("files", nargs="+", help="saved ads_get_ad_entities result(s), all pages")
    p.add_argument("--account", default="1672047784439131", help="if META_AD_ACCOUNT_ID unset")
    p.add_argument("--dry-run", action="store_true")

    p = sub.add_parser("add", help="record spend for a channel with no API")
    p.add_argument("--platform", choices=["manual"], default="manual")
    p.add_argument("--date", type=date.fromisoformat, required=True, help="YYYY-MM-DD")
    p.add_argument("--campaign", required=True)
    p.add_argument("--spend", type=float, required=True, help="USD")
    p.add_argument("--channel", help="the utm_source this spend bought, e.g. reddit")
    p.add_argument("--ad", help="optional ad/placement id")
    p.add_argument("--impressions", type=int)
    p.add_argument("--clicks", type=int)

    args = parser.parse_args()
    return {
        "status": cmd_status,
        "sync-meta": cmd_sync_meta,
        "ingest-mcp": cmd_ingest_mcp,
        "add": cmd_add,
    }[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
