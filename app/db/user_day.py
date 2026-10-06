"""The user's day: where "today" starts for caps and for the counts shown next to them.

Decision 2026-09-27 (Igor): the daily cap rolls over at the USER'S local midnight, not
the server's UTC one. Before this, enforcement (`check_can_apply`) counted from the UTC
date while the dashboard counted from the browser's midnight, so a Hawaii user had a
10-hour window in which one local day could spend two daily caps — and two per-platform
ban-safety budgets.

Where the zone comes from: the browser's IANA zone, sent by the dashboard with its
`/campaign/status` poll and stored in `user_timezones` — a table with RLS on and no
policies, so only this backend writes it (a `profiles` column would be writable by the
user through supabase-js). The enforcement side never takes a boundary from the request;
it reads the stored zone and computes midnight itself. A zone is accepted only if its
CURRENT offset is between UTC-11 and UTC-3 (the US and its neighbours — the product is
US-only). No zone on file → the UTC date, exactly the old behaviour.

Why a zone change is not a reset. Some zone in the band meets midnight every hour from
03:00 to 11:00 UTC, so a browser that reports a new zone each hour got a fresh day each
hour (skeptic 10-05: 200 applies in a "day"). A one-step look-back was not enough either:
a chain of westward changes a day apart still gave 3 caps in 32 h (skeptic 10-06: +21%
over 28 days). The rule now is one invariant, whatever the zones do:

    a new day starts no sooner than 23 h after the day it follows.

On a change, the start of the day in force is stored as `day_anchor`. Under the new zone
the day keeps that start until the first local midnight at least 23 h after it; from then
on it is that zone's plain midnight. 23 h, not 24, so the spring-forward day still rolls
over at midnight. The anchor carries over through any number of changes, so nothing is
forgotten. The cost: someone who really moves east waits for the first midnight 23 h
after their old day began — at most one long day per trip.
"""

import logging
from datetime import UTC, date, datetime, timedelta
from typing import NamedTuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.db.client import get_supabase

log = logging.getLogger(__name__)

_MIN_OFFSET = timedelta(hours=-11)
_MAX_OFFSET = timedelta(hours=-3)
MIN_DAY = timedelta(hours=23)


class ZoneOnFile(NamedTuple):
    zone: str
    day_anchor: datetime | None = None  # start of the day in force when the zone last changed


def _zone(tz: str | None) -> ZoneInfo | None:
    if not tz or len(tz) > 64:
        return None
    try:
        return ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError):
        return None


def valid_zone(tz: str | None) -> str | None:
    """The zone if it is a real IANA name inside the accepted offset band, else None."""
    zone = _zone(tz)
    if zone is None:
        return None
    offset = datetime.now(zone).utcoffset()
    if offset is None or not (_MIN_OFFSET <= offset <= _MAX_OFFSET):
        return None
    return tz


def _parse_ts(value) -> datetime | None:
    if not value:
        return None
    try:
        ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)


class _ReadError(Exception):
    pass


def _read(user_id: str) -> ZoneOnFile | None:
    try:
        rows = (
            get_supabase()
            .table("user_timezones")
            .select("zone,day_anchor")
            .eq("user_id", user_id)
            .limit(1)
            .execute()
            .data
        )
    except Exception as e:  # noqa: BLE001
        log.warning("user_timezones read failed for %s", user_id, exc_info=True)
        raise _ReadError from e
    # The band is checked when a zone is WRITTEN. On read only a real zone is required: a
    # stored zone whose offset drifts out of the band at DST must keep its day, not fall
    # back to the UTC date and lose the anchor.
    if not rows or _zone(rows[0].get("zone")) is None:
        return None
    return ZoneOnFile(rows[0]["zone"], _parse_ts(rows[0].get("day_anchor")))


def stored_zone(user_id: str) -> ZoneOnFile | None:
    try:
        return _read(user_id)
    except _ReadError:
        return None  # a cap must still be counted if this read fails: the UTC date


def remember_zone(user_id: str, tz: str | None, now: datetime | None = None) -> ZoneOnFile | None:
    """Store the browser's zone when it is valid and differs from what is on file, anchored
    to the day in force right now. Returns the zone record in force afterwards."""
    try:
        current = _read(user_id)
    except _ReadError:
        return None  # never overwrite a row we could not read — the anchor would be lost
    zone = valid_zone(tz)
    if not zone or (current and zone == current.zone):
        return current
    now = now or datetime.now(UTC)
    record = ZoneOnFile(zone, _start(current, now))
    try:
        get_supabase().table("user_timezones").upsert(
            {
                "user_id": user_id,
                "zone": record.zone,
                "day_anchor": record.day_anchor.isoformat(),
                "changed_at": now.isoformat(),
            },
            on_conflict="user_id",
        ).execute()
    except Exception:  # noqa: BLE001 — a failed write keeps the old zone, never breaks status
        log.warning("user_timezones write failed for %s", user_id, exc_info=True)
        return current
    return record


def _midnight(day: date, zone: ZoneInfo) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=zone).astimezone(UTC)


def _start(record: ZoneOnFile | None, now: datetime) -> datetime:
    """Start of the user's current day as an aware UTC instant."""
    if record is None or _zone(record.zone) is None:
        utc = now.astimezone(UTC)
        return datetime(utc.year, utc.month, utc.day, tzinfo=UTC)  # the UTC date in force
    zone = ZoneInfo(record.zone)
    plain = _midnight(now.astimezone(zone).date(), zone)
    anchor = record.day_anchor
    if anchor is None:
        return plain
    earliest = anchor + MIN_DAY
    first = _midnight(earliest.astimezone(zone).date(), zone)
    if first < earliest:
        first = _midnight(earliest.astimezone(zone).date() + timedelta(days=1), zone)
    return anchor if now < first else plain


def day_start(record: ZoneOnFile | str | None, now: datetime | None = None) -> str:
    """Start of the user's current day as a UTC ISO instant; the UTC date without a zone."""
    if isinstance(record, str):
        record = ZoneOnFile(record) if valid_zone(record) else None
    if record is None or _zone(record.zone) is None:
        return (now.date() if now else date.today()).isoformat()
    return _start(record, (now or datetime.now(UTC)).astimezone(UTC)).isoformat()


def user_day_start(user_id: str) -> str:
    return day_start(stored_zone(user_id))
