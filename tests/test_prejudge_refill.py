"""Judge what the person will see or the run will open next, and refill only for use.

prejudge_pool used to judge every unjudged row in the pool (up to 60 a pass) after every
sweep, including the nightly one for accounts that were not applying: an account that
opened nothing all week was judged 60 postings a night. These tests pin the rule that
replaced it — the background judge works on the dashboard's list and each platform's run
queue and nothing past them, judged lists cost nothing, and the nightly sweep runs only
for an account that applied since its last one.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from app import pool_sweep
from app.routers import jobs as jobs_router
from modules.fit_queue import has_current_verdict

PROFILE = {"id": "u1", "apply_mode": "standard", "keywords": []}


def _rows(n: int, platform: str = "lever", start: int = 0, prefix: str = "r") -> list[dict]:
    """n postings, the freshest first, each at its own company (no cap in play)."""
    now = datetime.now(UTC)
    return [
        {
            "id": f"{prefix}{i}",
            "title": f"Role {i}",
            "company": f"Company{prefix}{i}",
            "platform": platform,
            "link": f"https://{platform}.example/{prefix}{i}",
            "status": "new",
            "date_found": (now - timedelta(minutes=start + i)).isoformat(),
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


def _prejudge(rows, judge, retried=frozenset()):
    with (
        patch("app.db.profile.get_profile", return_value=PROFILE),
        patch.object(jobs_router.jobs_db, "get_jobs", return_value=rows),
        patch.object(jobs_router, "_retried_ids", return_value=set(retried)),
        patch("modules.ai_cover_letter.resume_text_for", return_value="resume " * 50),
        patch("modules.fit_queue.judge_pending", judge),
    ):
        return jobs_router.prejudge_pool("u1")


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


def test_each_platform_queue_the_run_opens_is_judged_even_off_the_list():
    # The list is full of fresher Indeed cards, but a campaign opens 15 Lever postings
    # (max_per_platform) — those must not wait for a judge while the run waits.
    rows = _rows(30, platform="indeed", prefix="i") + _rows(40, start=100)
    judge, judged = _judge()
    _prejudge(rows, judge)
    assert sorted(judged) == sorted(f"r{i}" for i in range(15))


def test_a_posting_sent_back_with_try_again_is_judged_where_the_list_shows_it():
    rows = _rows(40)
    judge, judged = _judge()
    _prejudge(rows, judge, retried={"r39"})
    assert "r39" in judged


def test_a_pass_out_of_time_starts_no_call():
    judge, judged = _judge()
    with patch.object(jobs_router, "PREJUDGE_SECS", 0):
        assert _prejudge(_rows(40), judge) == 0
    assert judged == []


def test_a_judge_outage_asks_each_row_once_per_pass():
    rows = _rows(40)
    judge, judged = _judge(undecided=True)
    _prejudge(rows, judge)
    assert sorted(judged) == sorted(f"r{i}" for i in range(jobs_router.DAILY_LIST_SIZE))


def test_discovery_charges_its_ai_calls_to_the_account_it_sweeps(ai_meter_rows):
    from types import SimpleNamespace

    from modules import ai_meter

    def discover(user_id):
        msg = SimpleNamespace(model="claude-haiku-4-5", usage=SimpleNamespace(input_tokens=10))
        ai_meter.record(msg, "job_score")

    with patch.object(jobs_router, "_discover_ats", discover):
        jobs_router._run_ats_discovery("u1")
    assert ai_meter_rows[0]["user_id"] == "u1"


# --------------------------------------------------------------------------- nightly sweep


def _scan(last_sweep: dict, used: dict):
    now = datetime.now(UTC)
    stamps = {uid: (now - timedelta(hours=h)).isoformat() for uid, h in last_sweep.items()}
    with (
        patch.object(pool_sweep.apps_db, "active_user_ids", return_value=list(used)),
        patch.object(pool_sweep.activity_db, "last_at", side_effect=lambda u, _p: stamps.get(u)),
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


def test_an_account_that_applied_since_its_last_sweep_is_swept():
    started, swept, _, counted = _scan(last_sweep={"busy": 30}, used={"busy": 4})
    assert started == 1
    swept.assert_called_once_with("busy")
    # Counted from the last sweep, not from some fixed window.
    since = datetime.fromisoformat(counted.call_args.args[1])
    now = datetime.now(UTC)
    assert now - timedelta(hours=31) < since < now - timedelta(hours=29)


def test_a_recent_sweep_is_not_repeated_whatever_was_used():
    started, swept, _, counted = _scan(last_sweep={"fresh": 2}, used={"fresh": 9})
    assert started == 0
    swept.assert_not_called()
    counted.assert_not_called()


def test_an_account_never_swept_counts_its_whole_activity_window():
    started, swept, _, counted = _scan(last_sweep={}, used={"new": 2})
    assert started == 1
    swept.assert_called_once_with("new")
    since = datetime.fromisoformat(counted.call_args.args[1])
    assert since < datetime.now(UTC) - timedelta(days=pool_sweep.POOL_SWEEP_ACTIVE_DAYS - 1)


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


def test_an_unjudged_indeed_card_on_the_list_does_not_wake_the_ats_judge():
    # The background judge only judges ATS rows; an Indeed card is decided by the
    # search-page judge or at apply time, so waking it would be a no-op every minute.
    rows = _rows(1, platform="indeed", prefix="i") + _rows(40, start=10)
    for r in rows[1 : jobs_router.DAILY_LIST_SIZE + 1]:
        r["fit_version"], r["fit_score"] = _version(), 90
    assert not _deck_kicks(rows)
