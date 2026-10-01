"""The prejudged queue — judge before the run, apply in the order the list shows.

Why these tests exist: until 09-30 the fit judge ran inside the run, one posting at a
time, and its verdict was thrown away. A 30-minute run opened 25 postings and applied to
none (09-22). modules/fit_queue.py moves the verdict up front; what has to hold:

  * the queue holds only what cleared the user's bar, freshest first — the score is a gate,
    not a rank (Igor, 09-30);
  * a judge outage leaves rows UNJUDGED (live judge decides them), never stored as 0;
  * a stored verdict is only reused for the profile it was judged against;
  * at most 2 applications per company per 60 days, history and queue counted together.
"""

import time
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import modules.fit_queue as fq
from modules.ai_fit_judge import verdict_version
from modules.fit_queue import build_queue, company_key, judge_pending

V = "v-current"


def _days_ago(n: int) -> str:
    return (datetime.now(UTC) - timedelta(days=n)).isoformat()


def _row(job_id, score=None, version=V, age=1, company=None):
    row = {
        "id": job_id,
        "title": f"Role {job_id}",
        "company": company or f"Company {job_id}",
        "description": "A real description of the job.",
        "date_found": _days_ago(age),
    }
    if score is not None:
        row.update(fit_score=score, fit_version=version, fit_reason=f"reason {job_id}")
    return row


# --- company key ------------------------------------------------------------------------


def test_one_employer_across_boards_is_one_key():
    assert company_key("DoorDash, Inc.") == company_key("DoorDash") == company_key("doordash LLC")
    assert company_key("The Coca-Cola Company") == "the coca cola"


def test_a_suffix_alone_is_still_a_name():
    assert company_key("Co") == "co"
    assert company_key("") == ""


# --- verdict version --------------------------------------------------------------------


def test_the_version_follows_what_the_judge_reads():
    base = {"apply_mode": "standard", "keywords": ["social media manager"]}
    v = verdict_version(base, "resume A")
    assert v == verdict_version(dict(base), "resume A")
    assert v != verdict_version(base, "resume B")  # resume edited
    assert v != verdict_version({**base, "apply_mode": "broad"}, "resume A")  # bar moved
    assert v != verdict_version({**base, "keywords": ["event manager"]}, "resume A")


def test_a_prompt_change_retires_every_stored_verdict():
    base = {"apply_mode": "standard"}
    v = verdict_version(base, "resume")
    with patch("modules.ai_fit_judge.PROMPT_VERSION", "next"):
        assert verdict_version(base, "resume") != v


# --- ordering -----------------------------------------------------------------------------


def test_freshest_first_whatever_the_score_and_below_bar_out():
    """Igor, 09-30: the score decides IN or OUT, never the position. A week-old 82 used to
    sit above this morning's 60 — the one likelier to still be open."""
    rows = [
        _row("old-ideal", score=82, age=5),
        _row("new-good", score=60, age=0),
        _row("newer-ideal", score=75, age=1),
        _row("below", score=40, age=0),
        _row("stale-version", score=90, version="v-old", age=3),
        _row("never-judged", age=2),
    ]
    out = build_queue(rows, V, bar=55, applied_companies=[], limit=30)
    assert [r["id"] for r in out["jobs"]] == [
        "new-good",
        "newer-ideal",
        "never-judged",  # unjudged for THIS profile → same freshness order, live judge decides
        "stale-version",
        "old-ideal",
    ]
    assert out["below_bar"] == 1
    assert out["passing"] == 3
    assert out["unjudged"] == 2


def test_a_stale_verdict_below_the_bar_does_not_drop_the_row():
    # Judged 40 against an OLD resume — that verdict is about someone else; re-judge, keep.
    rows = [_row("a", score=40, version="v-old")]
    out = build_queue(rows, V, bar=55, applied_companies=[], limit=30)
    assert [r["id"] for r in out["jobs"]] == ["a"]
    assert out["below_bar"] == 0


def test_the_limit_cuts_the_oldest_not_the_lowest():
    rows = [_row(str(i), score=95 - i * 10, age=4 - i) for i in range(5)]
    out = build_queue(rows, V, bar=55, applied_companies=[], limit=2)
    # "4" scores 55 and "3" 65 — the two freshest, so they lead over the 95 from four days ago.
    assert [r["id"] for r in out["jobs"]] == ["4", "3"]


# --- company cap ------------------------------------------------------------------------


def test_two_per_company_counting_what_was_already_sent():
    rows = [_row(f"dd{i}", score=80, age=i, company="DoorDash") for i in range(4)]
    out = build_queue(rows, V, bar=55, applied_companies=["DoorDash, Inc."], limit=30)
    assert [r["id"] for r in out["jobs"]] == ["dd0"]
    assert out["company_capped"] == 3


def test_two_per_company_inside_the_queue_itself():
    rows = [_row(f"dd{i}", score=80, age=i + 1, company="DoorDash") for i in range(4)]
    rows.append(_row("other", score=60, age=0))
    out = build_queue(rows, V, bar=55, applied_companies=[], limit=30)
    # The two FRESHEST DoorDash postings take the company's slots.
    assert [r["id"] for r in out["jobs"]] == ["other", "dd0", "dd1"]
    assert out["company_capped"] == 2


# --- judging ------------------------------------------------------------------------------


def _verdict(score, **extra):
    return {"fit_score": score, "reason": "fits", "judged": True, "judge_model": "haiku", **extra}


def test_judged_rows_are_stored_and_updated_in_place():
    rows = [_row("a"), _row("b", score=70)]  # b already has a current verdict
    with (
        patch("modules.ai_fit_judge.assess_fit", return_value=_verdict(66)) as judge,
        patch("app.db.jobs.save_fit_verdict", return_value=True) as save,
    ):
        n = judge_pending("u1", {}, rows, max_calls=10, deadline_s=5, resume_text="r", version=V)
    assert n == 1
    assert judge.call_count == 1  # b was not judged again
    save.assert_called_once_with("a", "u1", 66, "fits", "haiku", V)
    assert rows[0]["fit_score"] == 66 and rows[0]["fit_version"] == V


def test_a_judge_outage_leaves_the_row_unjudged_not_zero():
    rows = [_row("a")]
    outage = {"fit_score": 0, "decision": "skip", "judged": False, "fail_closed": True}
    with (
        patch("modules.ai_fit_judge.assess_fit", return_value=outage),
        patch("app.db.jobs.save_fit_verdict") as save,
    ):
        n = judge_pending("u1", {}, rows, max_calls=10, deadline_s=5, resume_text="r", version=V)
    assert n == 0
    save.assert_not_called()
    assert "fit_score" not in rows[0]
    out = build_queue(rows, V, bar=55, applied_companies=[], limit=30)
    assert [r["id"] for r in out["jobs"]] == ["a"]  # still offered — the live judge decides


def test_no_resume_no_stored_verdicts():
    rows = [_row("a")]
    with (
        patch("modules.ai_fit_judge.assess_fit", return_value=_verdict(10)) as judge,
        patch("app.db.jobs.save_fit_verdict") as save,
    ):
        n = judge_pending("u1", {}, rows, max_calls=10, deadline_s=5, resume_text="  ", version=V)
    assert n == 0
    judge.assert_not_called()
    save.assert_not_called()


def test_the_budget_caps_calls_freshest_first():
    rows = [_row(str(i), age=i) for i in range(10)]
    with (
        patch("modules.ai_fit_judge.assess_fit", return_value=_verdict(60)) as judge,
        patch("app.db.jobs.save_fit_verdict", return_value=True),
    ):
        judge_pending("u1", {}, rows, max_calls=3, deadline_s=5, resume_text="r", version=V)
    assert judge.call_count == 3
    assert {r["id"] for r in rows if "fit_score" in r} == {"0", "1", "2"}


def test_the_deadline_stops_waiting_but_late_verdicts_still_land():
    rows = [_row("slow")]

    def slow(**_kw):
        time.sleep(0.4)
        return _verdict(61)

    with (
        patch("modules.ai_fit_judge.assess_fit", side_effect=slow),
        patch("app.db.jobs.save_fit_verdict", return_value=True) as save,
    ):
        t0 = time.monotonic()
        n = judge_pending("u1", {}, rows, max_calls=5, deadline_s=0.05, resume_text="r", version=V)
        waited = time.monotonic() - t0
        assert n == 0 and waited < 0.35
        time.sleep(0.6)
        save.assert_called_once()
    assert not fq._IN_FLIGHT  # nothing stays reserved


def test_a_row_already_being_judged_is_not_judged_twice():
    rows = [_row("a"), _row("b")]
    with fq._IN_FLIGHT_LOCK:
        fq._IN_FLIGHT.add("a")
    try:
        with (
            patch("modules.ai_fit_judge.assess_fit", return_value=_verdict(60)) as judge,
            patch("app.db.jobs.save_fit_verdict", return_value=True),
        ):
            judge_pending("u1", {}, rows, max_calls=10, deadline_s=5, resume_text="r", version=V)
        assert judge.call_count == 1
    finally:
        with fq._IN_FLIGHT_LOCK:
            fq._IN_FLIGHT.discard("a")


# --- the endpoint -------------------------------------------------------------------------


def test_the_ats_queue_serves_the_prejudged_order():
    from app.routers import jobs as jobs_router

    class _User:
        id = "u1"

    def gh(job_id, age, company=None):
        return {
            **_row(job_id, age=age, company=company),
            "title": "Event Manager",
            "location": "Remote",
            "platform": "greenhouse",
            "status": "new",
            "link": f"https://job-boards.greenhouse.io/x/jobs/{job_id}",
        }

    pool = [
        gh("fresh-ok", 0),
        gh("older-ideal", 3),
        gh("poor", 1),
        gh("dup", 2, company="DoorDash"),
    ]
    scores = {"fresh-ok": 58, "older-ideal": 88, "poor": 20, "dup": 77}

    def judge(job, profile, resume_text, **_kw):
        jid = next(r["id"] for r in pool if r["company"] == job["company"])
        return _verdict(scores[jid])

    with (
        patch.object(jobs_router.jobs_db, "get_jobs", return_value=pool),
        patch("app.db.profile.get_profile", return_value={"keywords": ["event manager"]}),
        patch("modules.ai_cover_letter.resume_text_for", return_value="resume"),
        patch("modules.ai_fit_judge.assess_fit", side_effect=judge),
        patch("app.db.jobs.save_fit_verdict", return_value=True),
        patch("app.db.applications.companies_applied_since", return_value=["DoorDash", "DoorDash"]),
    ):
        out = jobs_router.get_ats_queue(platform="greenhouse", user=_User())
    assert [j["link"].rsplit("/", 1)[-1] for j in out["jobs"]] == ["fresh-ok", "older-ideal"]
    assert out["below_bar"] == 1
    assert out["company_capped"] == 1
    assert out["unjudged"] == 0
