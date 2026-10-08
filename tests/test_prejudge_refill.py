"""Judge what the person's list needs, and refill only what they used.

prejudge_pool used to judge every unjudged row in the pool (up to 60 a pass) after every
sweep, including the nightly one for accounts that were not applying: an account that
opened nothing all week was judged 60 postings a night. These tests pin the rule that
replaced it — the background judge works on the list (DAILY_LIST_SIZE rows) and nothing
past it, a judged list costs nothing, and the nightly sweep refills only as many fits as
the person used since the last sweep, skipping an idle account entirely.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from app import pool_sweep
from app.routers import jobs as jobs_router
from modules.fit_queue import has_current_verdict

PROFILE = {"id": "u1", "apply_mode": "standard", "keywords": []}


def _rows(n: int) -> list[dict]:
    """n postings, the freshest first, each at its own company (no cap in play)."""
    now = datetime.now(UTC)
    return [
        {
            "id": f"r{i}",
            "title": f"Role {i}",
            "company": f"Company{i}",
            "platform": "lever",
            "link": f"https://jobs.lever.co/c{i}/{i}",
            "status": "new",
            "date_found": (now - timedelta(minutes=i)).isoformat(),
        }
        for i in range(n)
    ]


def _judge(rejected: set[str] = frozenset(), undecided: bool = False):
    """A stand-in for judge_pending: gives every row it is handed a verdict (90 = fits,
    10 = below the bar), or none at all when the judge is down. Records what it judged."""
    judged: list[str] = []

    def judge_pending(user_id, profile, rows, *, max_calls, version, stats, **_):
        batch = [r for r in rows if not has_current_verdict(r, version)][:max_calls]
        for r in batch:
            judged.append(r["id"])
            if not undecided:
                r["fit_version"] = version
                r["fit_score"] = 10 if r["id"] in rejected else 90
        stats["submitted"] = len(batch)
        return 0 if undecided else len(batch)

    return judge_pending, judged


def _prejudge(rows, judge, fits_wanted=None):
    with (
        patch("app.db.profile.get_profile", return_value=PROFILE),
        patch.object(jobs_router, "_ats_candidates", return_value=(rows, rows, rows)),
        patch("modules.ai_cover_letter.resume_text_for", return_value="resume " * 50),
        patch("modules.fit_queue.judge_pending", judge),
    ):
        return jobs_router.prejudge_pool("u1", fits_wanted=fits_wanted)


def _version():
    from modules.ai_fit_judge import verdict_version

    return verdict_version(PROFILE, "resume " * 50)


def test_the_list_is_judged_and_nothing_past_it():
    rows = _rows(40)
    judge, judged = _judge()
    assert _prejudge(rows, judge) == jobs_router.DAILY_LIST_SIZE
    assert judged == [f"r{i}" for i in range(jobs_router.DAILY_LIST_SIZE)]


def test_a_judged_list_costs_nothing():
    rows = _rows(40)
    for r in rows[: jobs_router.DAILY_LIST_SIZE]:
        r["fit_version"], r["fit_score"] = _version(), 90
    judge, judged = _judge()
    assert _prejudge(rows, judge) == 0
    assert judged == []


def test_a_rejected_row_drops_out_and_the_next_one_is_judged():
    rows = _rows(40)
    judge, judged = _judge(rejected={f"r{i}" for i in range(5)})
    assert _prejudge(rows, judge) == 35
    # The five that moved up into the list were judged; the rest of the pool was not.
    assert judged[30:] == [f"r{i}" for i in range(30, 35)]


def test_the_nightly_refill_stops_at_what_the_person_used():
    rows = _rows(40)
    judge, judged = _judge()
    assert _prejudge(rows, judge, fits_wanted=3) == 3
    assert judged == ["r0", "r1", "r2"]


def test_the_nightly_refill_keeps_looking_until_it_finds_that_many_fits():
    rows = _rows(40)
    judge, judged = _judge(rejected={"r0", "r1"})
    _prejudge(rows, judge, fits_wanted=3)
    assert judged == ["r0", "r1", "r2", "r3", "r4"]


def test_a_judge_outage_asks_each_row_once_per_pass():
    rows = _rows(40)
    judge, judged = _judge(undecided=True)
    _prejudge(rows, judge)
    assert sorted(judged) == sorted(f"r{i}" for i in range(jobs_router.DAILY_LIST_SIZE))


def test_discovery_charges_its_ai_calls_to_the_account_it_sweeps(ai_meter_rows):
    from types import SimpleNamespace

    from modules import ai_meter

    def discover(user_id, fits_wanted):
        msg = SimpleNamespace(model="claude-haiku-4-5", usage=SimpleNamespace(input_tokens=10))
        ai_meter.record(msg, "job_score")

    with patch.object(jobs_router, "_discover_ats", discover):
        jobs_router._run_ats_discovery("u1")
    assert ai_meter_rows[0]["user_id"] == "u1"


# --------------------------------------------------------------------------- nightly sweep


def _scan(last_sweep: dict, used: dict):
    now = datetime.now(UTC)
    with (
        patch.object(pool_sweep.apps_db, "active_user_ids", return_value=list(used)),
        patch.object(
            pool_sweep.activity_db,
            "last_at",
            side_effect=lambda uid, _phase: (
                (now - timedelta(hours=last_sweep[uid])).isoformat() if uid in last_sweep else None
            ),
        ),
        patch.object(
            pool_sweep.apps_db, "count_today", side_effect=lambda uid, _s: used[uid]
        ) as counted,
        patch.object(pool_sweep.activity_db, "has_since", return_value=False),
        patch.object(pool_sweep.activity_db, "write") as claimed,
        patch.object(jobs_router, "_run_ats_discovery") as swept,
    ):
        started = pool_sweep.scan()
    return started, swept, claimed, counted


def test_an_account_that_applied_to_nothing_since_its_last_sweep_is_not_swept():
    started, swept, claimed, _ = _scan(last_sweep={"idle": 30}, used={"idle": 0})
    assert started == 0
    swept.assert_not_called()
    # Not claimed either: an application later today can still earn tonight's sweep.
    claimed.assert_not_called()


def test_the_sweep_refills_as_many_fits_as_were_used():
    started, swept, _, _ = _scan(last_sweep={"busy": 30}, used={"busy": 4})
    assert started == 1
    swept.assert_called_once_with("busy", fits_wanted=4)


def test_a_recent_sweep_is_not_repeated_whatever_was_used():
    started, swept, _, counted = _scan(last_sweep={"fresh": 2}, used={"fresh": 9})
    assert started == 0
    swept.assert_not_called()
    counted.assert_not_called()


def test_an_account_never_swept_counts_its_whole_activity_window():
    started, swept, _, _ = _scan(last_sweep={}, used={"new": 2})
    assert started == 1
    swept.assert_called_once_with("new", fits_wanted=2)


# --------------------------------------------------------------------------- dashboard read


def _deck_kicks(rows) -> bool:
    with (
        patch.object(jobs_router.jobs_db, "get_jobs", return_value=rows),
        patch("app.db.profile.get_profile", return_value=PROFILE),
        patch.object(jobs_router, "_deck_resume_text", return_value="resume " * 50),
        patch.object(jobs_router, "_prejudge_in_background") as kick,
    ):
        jobs_router.get_deck(user=type("U", (), {"id": "u1"})())
    return kick.called


def test_unjudged_rows_past_the_list_do_not_restart_the_background_judge():
    rows = _rows(40)
    for r in rows[: jobs_router.DAILY_LIST_SIZE]:
        r["fit_version"], r["fit_score"] = _version(), 90
    assert not _deck_kicks(rows)


def test_an_unjudged_row_on_the_list_starts_the_background_judge():
    rows = _rows(40)
    for r in rows[1 : jobs_router.DAILY_LIST_SIZE]:
        r["fit_version"], r["fit_score"] = _version(), 90
    assert _deck_kicks(rows)
