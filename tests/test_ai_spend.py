"""AI $ per application and when to raise the alarm — the numbers the admin board and the
daily report share (modules/ai_spend.py, scripts/ai_cost_report.py)."""

from unittest.mock import patch

from modules import ai_spend


def _day(day, cost, user="u1", purpose="fit_judge", model="claude-haiku-4-5", calls=1, **kw):
    return {
        "day": day,
        "user_id": user,
        "purpose": purpose,
        "model": model,
        "calls": calls,
        "cost_usd": cost,
        "unpriced_calls": kw.get("unpriced", 0),
        "input_tokens": kw.get("input_tokens", 0),
        "output_tokens": 0,
        "cache_read_tokens": kw.get("cache_read", 0),
        "cache_write_tokens": 0,
    }


def _app(day, user="u1"):
    return {"user_id": user, "date_applied": f"{day}T12:00:00+00:00"}


def test_the_ceiling_keeps_the_heaviest_paying_user_above_water():
    # $39 a month, 30% to the affiliate, 30 applications a day for 30 days.
    assert round(39 * 0.7 / 900, 4) == ai_spend.CEILING_PER_APPLICATION_USD


def test_cost_per_application_is_spend_over_applications_per_day():
    s = ai_spend.summarize(
        [_day("2026-10-07", "0.30"), _day("2026-10-07", 0.10, purpose="cover_letter")],
        [_app("2026-10-07")] * 4 + [_app("2026-10-09")],  # outside the window: ignored
        "2026-10-06",
        "2026-10-07",
    )
    assert [d["day"] for d in s["days"]] == ["2026-10-06", "2026-10-07"]
    assert s["days"][1]["per_application"] == 0.1
    assert s["days"][0]["per_application"] is None  # no applications is not $0 each
    assert s["applications"] == 4
    assert s["by_purpose"][0]["purpose"] == "fit_judge"
    assert s["by_purpose"][0]["cost"] == 0.3


def test_spend_with_no_account_is_counted_apart():
    s = ai_spend.summarize(
        [_day("2026-10-07", 1.0), _day("2026-10-07", 3.0, user=None)],
        [],
        "2026-10-07",
        "2026-10-07",
    )
    assert s["unattributed_cost"] == 3.0
    assert any("charged to no account" in a for a in ai_spend.alerts(s))


def test_cache_share_is_read_tokens_over_all_prompt_tokens():
    s = ai_spend.summarize(
        [_day("2026-10-07", 0, input_tokens=970, cache_read=30)], [], "2026-10-07", "2026-10-07"
    )
    assert s["cache_read_share"] == 0.03


def test_applications_from_before_the_ledger_do_not_water_down_the_price():
    # The ledger started mid-day on the 6th: spend before it was never recorded, so the
    # applications from before it must not divide the spend that was.
    apps = [_app("2026-10-01")] * 50 + [
        {"user_id": "u1", "date_applied": "2026-10-06T08:00:00+00:00"},
        {"user_id": "u1", "date_applied": "2026-10-06T20:00:00+00:00"},
        _app("2026-10-07"),
    ]
    s = ai_spend.summarize(
        [_day("2026-10-06", 0.03), _day("2026-10-07", 0.03)],
        apps,
        "2026-10-01",
        "2026-10-07",
        metered_since="2026-10-06T12:00:00+00:00",
    )
    assert s["applications"] == 2
    assert s["per_application"] == 0.03
    assert s["days"][0] == {**s["days"][0], "metered": False, "per_application": None}


def test_the_first_week_of_the_ledger_raises_no_false_jump():
    apps = [_app(f"2026-10-0{d}") for d in range(1, 9) for _ in range(10)]
    s = ai_spend.summarize(
        [_day("2026-10-08", 0.30)], apps, "2026-10-01", "2026-10-08", "2026-10-08T00:00:00+00:00"
    )
    assert not any("jumped" in a for a in ai_spend.alerts(s))


def test_a_quiet_day_under_the_ceiling_raises_nothing():
    s = ai_spend.summarize(
        [_day("2026-10-07", 0.20)], [_app("2026-10-07")] * 10, "2026-10-07", "2026-10-07"
    )
    assert ai_spend.alerts(s) == []


def test_a_day_over_the_ceiling_is_named():
    s = ai_spend.summarize(
        [_day("2026-10-07", 2.40)], [_app("2026-10-07")] * 10, "2026-10-07", "2026-10-07"
    )
    assert any("over the" in a for a in ai_spend.alerts(s))


def test_a_jump_against_the_week_before_is_named_even_under_the_ceiling():
    daily = [_day(f"2026-10-0{d}", 0.10) for d in range(1, 8)] + [_day("2026-10-08", 0.25)]
    apps = [_app(f"2026-10-0{d}") for d in range(1, 9) for _ in range(10)]
    s = ai_spend.summarize(daily, apps, "2026-10-01", "2026-10-08")
    found = ai_spend.alerts(s, ceiling=1.0)
    assert any("jumped" in a for a in found)


def test_an_account_that_costs_money_and_applies_to_nothing_is_named():
    s = ai_spend.summarize(
        [_day("2026-10-07", 0.40, user="idle-account-1")], [], "2026-10-07", "2026-10-07"
    )
    assert any("idle-acc" in a and "0 applications" in a for a in ai_spend.alerts(s))


def test_calls_of_an_unpriced_model_are_named():
    s = ai_spend.summarize(
        [_day("2026-10-07", 0, unpriced=2)], [_app("2026-10-07")], "2026-10-07", "2026-10-07"
    )
    assert any("no price" in a for a in ai_spend.alerts(s))


# --------------------------------------------------------------------------- admin board


def test_the_admin_board_reads_dollars_from_the_ledger():
    from app.routers import admin

    with (
        patch.object(admin.ai_calls_db, "daily", return_value=[_day("2026-10-07", "0.60")]),
        patch.object(admin.ai_calls_db, "first_at", return_value="2026-10-07T00:00:00+00:00"),
    ):
        section = admin._section_ai_cost(
            [_app("2026-10-07")] * 3,
            "2026-10-07T00:00:00Z",
            "2026-10-07T23:59:59Z",
            "2026-10-07",
            "2026-10-07",
        )
    metrics = {m["key"]: m["value"] for m in section["metrics"]}
    assert metrics["ai_spend"] == 0.6
    assert metrics["cost_per_application"] == 0.2
    assert "Metered since 2026-10-07" in section["subtitle"]
    assert section["tables"][0]["rows"][0]["cost_per_app"] == 0.2


# --------------------------------------------------------------------------- daily report


def _report(daily, apps, *args):
    import scripts.ai_cost_report as report

    with (
        patch.object(report.ai_calls_db, "daily", return_value=daily),
        patch.object(report.ai_calls_db, "first_at", return_value="2026-10-01T00:00:00+00:00"),
        patch.object(report, "_applications", return_value=apps),
    ):
        return report.main(["--to", "2026-10-07", "--days", "1", *args])


def test_the_report_exits_1_when_there_is_something_to_look_at(capsys):
    assert _report([_day("2026-10-07", 3.0)], [_app("2026-10-07")]) == 1
    assert "over the" in capsys.readouterr().out


def test_the_report_exits_0_on_a_quiet_day(capsys):
    assert _report([_day("2026-10-07", 0.1)], [_app("2026-10-07")] * 10) == 0
    assert "no alerts" in capsys.readouterr().out


def test_an_unreadable_ledger_is_exit_2_never_all_clear(capsys):
    import scripts.ai_cost_report as report

    with patch.object(report.ai_calls_db, "first_at", side_effect=RuntimeError("no table")):
        assert report.main(["--to", "2026-10-07"]) == 2
    assert "ledger unreadable" in capsys.readouterr().err


def test_the_bill_is_summed_from_cents_per_day():
    import scripts.ai_cost_report as report

    page = {
        "data": [
            {
                "starting_at": "2026-10-07T00:00:00Z",
                "results": [{"amount": "123.5", "currency": "USD"}, {"amount": "76.5"}],
            }
        ],
        "has_more": False,
        "next_page": None,
    }

    class Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return page

    with patch("httpx.get", return_value=Resp()):
        assert report.billed_by_day("2026-10-07", "2026-10-07", "k") == {"2026-10-07": 2.0}


def _compare(moment: str, today: str):
    import scripts.ai_cost_report as report

    windows = []

    def summary(from_day, to_day, _since):
        windows.append((from_day, to_day))
        return ai_spend.summarize([], [], from_day, to_day)

    class _Now:
        @staticmethod
        def now(tz=None):
            from datetime import datetime

            return datetime.fromisoformat(f"{today}T12:00:00+00:00")

        fromisoformat = staticmethod(__import__("datetime").datetime.fromisoformat)

    with (
        patch.object(report.ai_calls_db, "first_at", return_value=None),
        patch.object(report, "_summary", summary),
        patch.object(report, "datetime", _Now),
    ):
        code = report.main(["--compare", moment, "--days", "2"])
    return code, windows


def test_compare_leaves_the_deploy_day_out_of_both_sides():
    code, windows = _compare("2026-10-07T21:03:00Z", "2026-10-12")
    assert code == 0
    assert windows == [("2026-10-05", "2026-10-06"), ("2026-10-08", "2026-10-09")]


def test_compare_reads_a_local_time_on_its_utc_day():
    # 22:00 in Los Angeles on the 7th is the 8th in UTC.
    _, windows = _compare("2026-10-07T22:00:00-07:00", "2026-10-12")
    assert windows[1] == ("2026-10-09", "2026-10-10")


def test_compare_on_a_deploy_from_today_says_so_instead_of_an_empty_after(capsys):
    code, windows = _compare("2026-10-12T09:00:00Z", "2026-10-12")
    assert code == 0 and windows == []
    assert "no complete UTC day" in capsys.readouterr().out


def test_reconcile_flags_a_ledger_above_the_bill_as_a_price_problem():
    import scripts.ai_cost_report as report

    s = ai_spend.summarize([_day("2026-10-07", 2.0)], [], "2026-10-07", "2026-10-07")
    found = report.print_reconcile(s, {"2026-10-07": 1.0})
    assert any("PRICES" in a for a in found)
    assert report.print_reconcile(s, {"2026-10-07": 2.1}) == []
