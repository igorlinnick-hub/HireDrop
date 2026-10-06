"""The user's day (decision 2026-09-27): the cap rolls over at the user's local midnight,
from a zone stored server-side — never from a boundary the request supplies."""

import random
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from app.db import user_day

# 2026-10-05 23:00 UTC = 13:00 in Honolulu, 18:00 in Chicago — same local day for both,
# already the NEXT day in UTC terms only after midnight UTC.
NOW = datetime(2026, 10, 5, 23, 0, tzinfo=UTC)


def test_honolulu_day_starts_at_its_midnight():
    assert user_day.day_start("Pacific/Honolulu", NOW) == "2026-10-05T10:00:00+00:00"


def test_after_utc_midnight_hawaii_is_still_on_the_same_day():
    # The bug: 01:00 UTC on the 6th is 15:00 on the 5th in Honolulu. UTC enforcement
    # started a fresh cap here; the user's day must not.
    later = datetime(2026, 10, 6, 1, 0, tzinfo=UTC)
    assert user_day.day_start("Pacific/Honolulu", later) == "2026-10-05T10:00:00+00:00"


def test_dst_is_followed():
    summer = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    winter = datetime(2026, 12, 1, 12, 0, tzinfo=UTC)
    assert user_day.day_start("America/Chicago", summer) == "2026-07-01T05:00:00+00:00"
    assert user_day.day_start("America/Chicago", winter) == "2026-12-01T06:00:00+00:00"


def test_no_zone_is_the_old_utc_date():
    assert user_day.day_start(None, NOW) == "2026-10-05"


@pytest.mark.parametrize(
    "tz",
    [
        "Pacific/Kiritimati",  # UTC+14 — would let a forger pick the latest midnight on Earth
        "Europe/London",
        "Asia/Tokyo",
        "Etc/UTC",
        "Not/AZone",
        "",
        None,
        "America/" + "x" * 80,
    ],
)
def test_zones_outside_the_band_are_refused(tz):
    assert user_day.valid_zone(tz) is None
    assert user_day.day_start(tz, NOW) == "2026-10-05"


@pytest.mark.parametrize(
    "tz", ["Pacific/Honolulu", "America/Anchorage", "America/Los_Angeles", "America/New_York"]
)
def test_us_zones_are_accepted(tz):
    assert user_day.valid_zone(tz) == tz


class _Table:
    """In-memory stand-in for the user_timezones row of one user."""

    def __init__(self, row=None, fail_read=False):
        self.row = row
        self.fail_read = fail_read
        self.writes = []

    def table(self, name):
        assert name == "user_timezones"
        return _Query(self)


class _Query:
    def __init__(self, t):
        self.t, self.up = t, None

    def select(self, *_):
        return self

    def eq(self, *_):
        return self

    def limit(self, *_):
        return self

    def upsert(self, row, on_conflict=None):
        assert on_conflict == "user_id"
        self.up = dict(row)
        return self

    def execute(self):
        res = MagicMock()
        if self.up is None:
            if self.t.fail_read:
                raise RuntimeError("relation user_timezones does not exist")
            res.data = [dict(self.t.row)] if self.t.row else []
        else:
            self.t.row = self.up
            self.t.writes.append(self.up)
            res.data = [self.up]
        return res


def _with(table):
    return patch("app.db.user_day.get_supabase", return_value=table)


def test_stored_zone_reads_zone_and_anchor():
    t = _Table({"zone": "Pacific/Honolulu", "day_anchor": "2026-10-05T00:00:00Z"})
    with _with(t):
        rec = user_day.stored_zone("u1")
    assert rec == user_day.ZoneOnFile("Pacific/Honolulu", datetime(2026, 10, 5, tzinfo=UTC))


def test_stored_zone_refuses_a_non_zone_but_keeps_one_that_drifted_out_of_the_band():
    with _with(_Table({"zone": "Not/AZone", "day_anchor": None})):
        assert user_day.stored_zone("u1") is None
    # Validated against the band when written; on read a real zone keeps its day even if
    # DST has since moved its offset (America/Miquelon, skeptic 10-06).
    with _with(_Table({"zone": "Asia/Tokyo", "day_anchor": None})):
        assert user_day.stored_zone("u1").zone == "Asia/Tokyo"


def test_stored_zone_read_failure_falls_back_instead_of_raising(caplog):
    with _with(_Table(fail_read=True)):
        assert user_day.stored_zone("u1") is None
    assert "user_timezones read failed" in caplog.text


def test_remember_zone_writes_only_a_valid_change_anchored_to_the_day_in_force():
    t = _Table({"zone": "Pacific/Honolulu", "day_anchor": None})
    with _with(t):
        assert user_day.remember_zone("u1", "Pacific/Honolulu", NOW).zone == "Pacific/Honolulu"
        assert user_day.remember_zone("u1", "Asia/Tokyo", NOW).zone == "Pacific/Honolulu"
        assert t.writes == []
        rec = user_day.remember_zone("u1", "America/Chicago", NOW)
    # 23:00 UTC Oct 5: the Honolulu day in force began at 10:00 UTC.
    hnl_midnight = datetime(2026, 10, 5, 10, tzinfo=UTC)
    assert rec == user_day.ZoneOnFile("America/Chicago", hnl_midnight)
    assert t.writes == [
        {
            "user_id": "u1",
            "zone": "America/Chicago",
            "day_anchor": hnl_midnight.isoformat(),
            "changed_at": NOW.isoformat(),
        }
    ]


def test_remember_zone_never_overwrites_a_row_it_could_not_read():
    t = _Table(fail_read=True)
    with _with(t):
        assert user_day.remember_zone("u1", "America/Chicago", NOW) is None
    assert t.writes == []


def test_remember_zone_write_failure_keeps_the_old_zone():
    t = _Table()
    t.table = MagicMock(side_effect=[_Query(t), RuntimeError("PGRST205")])
    with _with(t):
        assert user_day.remember_zone("u1", "America/Chicago", NOW) is None


def test_a_zone_change_does_not_reset_the_day():
    # 10:59 UTC: Sao Paulo's Oct 5 began at 03:00. Moving to Pago Pago (midnight 11:00)
    # must not make 11:00 a fresh day.
    rec = user_day.ZoneOnFile("Pacific/Pago_Pago", datetime(2026, 10, 5, 3, tzinfo=UTC))
    assert user_day.day_start(rec, datetime(2026, 10, 5, 11, 1, tzinfo=UTC)) == (
        "2026-10-05T03:00:00+00:00"
    )
    # The first Pago Pago midnight >= 23 h after 03:00 is Oct 6 11:00.
    assert user_day.day_start(rec, datetime(2026, 10, 6, 10, 59, tzinfo=UTC)) == (
        "2026-10-05T03:00:00+00:00"
    )
    assert user_day.day_start(rec, datetime(2026, 10, 6, 11, 0, tzinfo=UTC)) == (
        "2026-10-06T11:00:00+00:00"
    )
    assert user_day.day_start(rec, datetime(2026, 10, 9, 12, 0, tzinfo=UTC)) == (
        "2026-10-09T11:00:00+00:00"
    )


def test_first_zone_continues_the_utc_day_it_replaces():
    t = _Table()
    with _with(t):
        rec = user_day.remember_zone("u1", "Pacific/Honolulu", NOW)  # 13:00 HST Oct 5
    assert rec.day_anchor == datetime(2026, 10, 5, tzinfo=UTC)
    assert user_day.day_start(rec, NOW + timedelta(minutes=1)) == "2026-10-05T00:00:00+00:00"
    assert user_day.day_start(rec, NOW + timedelta(hours=12)) == "2026-10-06T10:00:00+00:00"


def test_spring_forward_day_still_rolls_over_at_midnight():
    # Mar 8 2026 is 23 h long in Chicago; an anchor at its midnight must not push Mar 9.
    rec = user_day.ZoneOnFile("America/Chicago", datetime(2026, 3, 8, 6, tzinfo=UTC))
    assert user_day.day_start(rec, datetime(2026, 3, 9, 5, 0, tzinfo=UTC)) == (
        "2026-03-09T05:00:00+00:00"
    )


_BAND = [
    "America/Asuncion",
    "America/Sao_Paulo",
    "America/St_Johns",
    "America/Halifax",
    "America/New_York",
    "America/Chicago",
    "America/Denver",
    "America/Phoenix",
    "America/Los_Angeles",
    "America/Anchorage",
    "Pacific/Honolulu",
    "Pacific/Pago_Pago",
]


def _boundary(s):
    return datetime.fromisoformat(s if "T" in s else s + "T00:00:00+00:00")


def _simulate(pick_zone, start=datetime(2026, 10, 5, tzinfo=UTC), hours=72, cap=30, step=15):
    """Apply as fast as the cap allows while the browser reports pick_zone(t) each step.
    Returns (applies, the distinct day starts seen, in order)."""
    t, end = start, start + timedelta(hours=hours)
    applied: list[datetime] = []
    starts: list[datetime] = []
    table = _Table()
    with _with(table):
        while t < end:
            user_day.remember_zone("u1", pick_zone(t), t)
            b = _boundary(user_day.day_start(user_day.stored_zone("u1"), t))
            if not starts or b != starts[-1]:
                starts.append(b)
            used = sum(1 for a in applied if a >= b)
            applied.extend([t] * max(0, cap - used))
            t += timedelta(minutes=step)
    return len(applied), starts


def _honest(start=datetime(2026, 10, 5, tzinfo=UTC), hours=72):
    return max(_simulate(lambda t, z=z: z, start, hours)[0] for z in _BAND)


def _freshest(t):
    # The zone whose midnight passed most recently — the hourly ratchet (skeptic 10-05).
    return min(_BAND, key=lambda z: t.astimezone(ZoneInfo(z)).time())


def test_hourly_zone_hopping_gains_nothing_over_one_honest_zone():
    assert _simulate(_freshest)[0] <= _honest()


def test_flipping_two_zones_gains_nothing_over_one_honest_zone():
    flip = _simulate(lambda t: "America/Sao_Paulo" if t.hour % 2 else "Pacific/Pago_Pago")
    assert flip[0] <= _honest()


def _schedule(events, start):
    def pick(t):
        hours = (t - start) / timedelta(hours=1)
        zone = None
        for at, z in events:
            if hours >= at:
                zone = z
        return zone

    return pick


def test_westward_chain_gains_nothing():
    # Skeptic 10-06: with a one-zone look-back this schedule got 300 in 7 days vs 240 —
    # three caps in 32 h, and the gain grew with the horizon.
    start = datetime(2026, 10, 5, tzinfo=UTC)
    events = [
        (0, "America/Asuncion"),
        (53, "America/Anchorage"),
        (78, "Pacific/Pago_Pago"),
        (103, "America/Phoenix"),
        (129, "Pacific/Honolulu"),
        (153, "Pacific/Pago_Pago"),
    ]
    applies, _ = _simulate(_schedule(events, start), start, hours=168, step=30)
    assert applies <= _honest(start, 168)


@pytest.mark.parametrize(
    "start",
    [
        datetime(2026, 10, 5, tzinfo=UTC),
        datetime(2026, 10, 29, tzinfo=UTC),  # US fall-back Nov 1
        datetime(2026, 3, 5, tzinfo=UTC),  # US spring-forward Mar 8
    ],
)
def test_no_zone_schedule_starts_a_day_sooner_than_23h_after_the_last(start):
    # The invariant itself, under random schedules: day starts never move back, and two
    # consecutive ones are at least 23 h apart.
    rng = random.Random(start.toordinal())
    for _ in range(25):
        events = sorted((rng.uniform(0, 160), rng.choice(_BAND)) for _ in range(rng.randint(1, 12)))
        _, starts = _simulate(_schedule(events, start), start, hours=168, step=30)
        for a, b in zip(starts, starts[1:], strict=False):
            assert b - a >= timedelta(hours=23), (events, a, b)


def test_check_can_apply_counts_from_the_stored_midnight():
    from app.db import subscriptions

    with (
        patch("app.db.subscriptions.user_day_start", return_value="2026-10-05T10:00:00+00:00"),
        patch("app.db.subscriptions.get_tier", return_value="pro"),
        patch("app.db.subscriptions.get_submit_mode", return_value="auto"),
        patch("app.db.subscriptions.apps_db.count_today", return_value=3) as count,
        patch(
            "app.db.subscriptions.apps_db.count_today_by_platform", return_value={}
        ) as by_platform,
    ):
        res = subscriptions.check_can_apply("u1", "indeed")
    assert res["allowed"] is True
    count.assert_called_once_with("u1", "2026-10-05T10:00:00+00:00")
    by_platform.assert_called_once_with("u1", "2026-10-05T10:00:00+00:00")


def _status(client, query, zone_on_file):
    rec = user_day.ZoneOnFile(zone_on_file) if zone_on_file else None
    with (
        patch("app.routers.campaign.remember_zone", return_value=rec) as remember,
        patch("app.routers.campaign.apps_db.count_today", return_value=0) as count,
        patch("app.routers.campaign.apps_db.count_today_by_platform", return_value={}),
    ):
        client.get(f"/api/v1/campaign/status{query}")
    return count, remember


def test_status_counts_from_the_stored_zone_not_the_client_since(auth_client):
    count, _ = _status(auth_client, "?since=2026-01-01T00:00:00Z", "Pacific/Honolulu")
    boundary = count.call_args.args[1]
    assert boundary != "2026-01-01T00:00:00Z"
    assert boundary.endswith("10:00:00+00:00")


def test_status_falls_back_to_client_since_without_a_zone(auth_client):
    count, _ = _status(auth_client, "?since=2026-10-05T10:00:00Z", None)
    assert count.call_args.args[1] == "2026-10-05T10:00:00Z"


def test_status_passes_the_browser_zone_to_be_remembered(auth_client):
    _, remember = _status(auth_client, "?tz=America/Chicago", None)
    assert remember.call_args.args[1] == "America/Chicago"
