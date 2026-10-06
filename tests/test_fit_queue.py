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
    assert company_key("The Coca-Cola Company") == "thecocacola"


def test_a_greenhouse_board_token_is_the_same_employer():
    # Greenhouse rows carry the board token as company; Indeed carries the name.
    assert company_key("doordashusa") == company_key("DoorDash, Inc.")
    assert company_key("Muckrack") == company_key("Muck Rack")
    assert company_key("grafanalabs") == company_key("Grafana Labs")
    assert company_key("stripecareers") == company_key("Stripe")


def test_a_short_name_keeps_its_tail():
    assert company_key("Medusa") == "medusa"
    assert company_key("Jobs") == "jobs"


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


def test_one_per_company_counting_what_was_already_sent():
    rows = [_row(f"dd{i}", score=80, age=i, company="DoorDash") for i in range(4)]
    rows.append(_row("other", score=60, age=0))
    out = build_queue(rows, V, bar=55, applied_companies=["DoorDash, Inc."], limit=30)
    assert [r["id"] for r in out["jobs"]] == ["other"]
    assert out["company_capped"] == 4


def test_one_per_company_inside_the_queue_itself():
    # Live 10-05: two DoorDash postings ran back to back in one Greenhouse run.
    rows = [_row(f"dd{i}", score=80, age=i + 1, company="DoorDash") for i in range(4)]
    rows.append(_row("other", score=60, age=0))
    out = build_queue(rows, V, bar=55, applied_companies=[], limit=30)
    # The FRESHEST DoorDash posting takes the company's slot.
    assert [r["id"] for r in out["jobs"]] == ["other", "dd0"]
    assert out["company_capped"] == 3


def test_a_retried_posting_passes_the_cap_and_goes_first():
    # Skeptic on #353: the company's OTHER open hand-backs held its one slot, so the
    # posting the person pressed "Try again" on was cut while the UI said "back in the
    # queue". A retried row skips the cap, goes first, and still takes the slot.
    rows = [
        _row("dd-new", score=80, age=0, company="DoorDash"),
        _row("dd-retried", score=80, age=5, company="DoorDash"),
        _row("other", score=60, age=1),
    ]
    out = build_queue(
        rows,
        V,
        bar=55,
        applied_companies=["Doordashusa"] * 3,
        limit=30,
        retried_ids={"dd-retried"},
    )
    assert [r["id"] for r in out["jobs"]] == ["dd-retried", "other"]
    assert out["company_capped"] == 1


def test_a_retried_posting_below_the_bar_stays_out():
    # The live judge would skip it at apply time anyway (no new hand-back, so requeued_at
    # stays set): letting it in only buys a judge call on every run and a false "fit".
    rows = [_row("retried", score=40, age=2), _row("ok", score=70, age=0)]
    out = build_queue(rows, V, bar=55, applied_companies=[], limit=30, retried_ids={"retried"})
    assert [r["id"] for r in out["jobs"]] == ["ok"]
    assert out["below_bar"] == 1


def test_every_retried_posting_at_one_company_goes():
    rows = [_row(f"dd{i}", score=80, age=i, company="DoorDash") for i in range(3)]
    out = build_queue(rows, V, bar=55, applied_companies=[], limit=30, retried_ids={"dd1", "dd2"})
    # dd0 is fresher but not retried: the retried two take the slot first.
    assert [r["id"] for r in out["jobs"]] == ["dd1", "dd2"]
    assert out["company_capped"] == 1


def test_without_retries_the_cap_is_unchanged():
    rows = [_row("dd", score=80, age=0, company="DoorDash")]
    out = build_queue(rows, V, bar=55, applied_companies=["DoorDash"], limit=30)
    assert out["jobs"] == [] and out["company_capped"] == 1


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
        patch("app.db.applications.companies_applied_since", return_value=["DoorDash"]),
    ):
        out = jobs_router.get_ats_queue(platform="greenhouse", user=_User())
    assert [j["link"].rsplit("/", 1)[-1] for j in out["jobs"]] == ["fresh-ok", "older-ideal"]
    assert out["below_bar"] == 1
    assert out["company_capped"] == 1
    assert out["unjudged"] == 0


def test_a_hand_back_holds_the_company_slot():
    # Live 10-01…10-05: DoorDash forms stalled 4 times in 5 days and, never counted as
    # "applied", the company came back every run. A hand-back now holds the slot.
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
        patch("app.db.applications.companies_applied_since", return_value=[]),
        patch("app.db.handbacks.companies_handed_back_since", return_value=["Doordashusa"]),
    ):
        out = jobs_router.get_ats_queue(platform="greenhouse", user=_User())
    assert [j["link"].rsplit("/", 1)[-1] for j in out["jobs"]] == ["fresh-ok", "older-ideal"]
    assert out["below_bar"] == 1
    assert out["company_capped"] == 1
    assert out["unjudged"] == 0


def test_hand_back_history_leaves_out_what_the_person_sent_back(real_companies_handed_back_since):
    # "Try again" / answered questions set requeued_at: that retry is the person's call
    # and must reach the queue, so those rows must not hold the company slot.
    from unittest.mock import MagicMock

    from app.db import handbacks as hb_db

    q = MagicMock()
    for m in ("table", "select", "eq", "gte", "is_", "order", "range"):
        getattr(q, m).return_value = q
    q.execute.return_value = MagicMock(data=[{"company": "DoorDash"}])
    with patch.object(hb_db, "get_supabase", return_value=q):
        out = real_companies_handed_back_since("u1", 60)
    assert out == ["DoorDash"]
    q.table.assert_called_with("handbacks")
    q.is_.assert_any_call("requeued_at", "null")
    q.eq.assert_any_call("user_id", "u1")


def test_try_again_reaches_the_queue_past_the_companys_other_hand_backs():
    # The skeptic's case end to end: DoorDash has two open hand-backs, the person retried
    # one. Its posting must be in the queue, first; the other DoorDash posting stays out.
    from app.routers import jobs as jobs_router

    class _User:
        id = "u1"

    def gh(job_id, age, company=None):
        return {
            **_row(job_id, score=80, age=age, company=company),
            "title": "Event Manager",
            "location": "Remote",
            "platform": "greenhouse",
            "status": "new",
            "link": f"https://job-boards.greenhouse.io/doordashusa/jobs/{job_id}",
        }

    pool = [gh("7001", 0, "DoorDash"), gh("7002", 4, "DoorDash"), gh("7003", 1, "Acme")]
    with (
        patch.object(jobs_router.jobs_db, "get_jobs", return_value=pool),
        patch("app.db.profile.get_profile", return_value={"keywords": ["event manager"]}),
        patch("modules.ai_cover_letter.resume_text_for", return_value="resume"),
        patch("modules.fit_queue.has_current_verdict", return_value=True),
        patch("app.db.applications.companies_applied_since", return_value=[]),
        patch(
            "app.db.handbacks.companies_handed_back_since",
            return_value=["Doordashusa", "Doordashusa"],
        ),
        patch(
            "app.db.handbacks.requeued_urls",
            # The hand-back URL is the form screen, not the saved posting link.
            return_value=["https://job-boards.greenhouse.io/doordashusa/jobs/7002?gh_src=abc#app"],
        ),
    ):
        out = jobs_router.get_ats_queue(platform="greenhouse", user=_User())
    assert [j["link"].rsplit("/", 1)[-1] for j in out["jobs"]] == ["7002", "7003"]
    assert out["company_capped"] == 1


def test_requeued_urls_reads_only_open_retried_rows(real_requeued_urls):
    from unittest.mock import MagicMock

    from app.db import handbacks as hb_db

    q, negated = MagicMock(), MagicMock()
    for m in ("table", "select", "eq", "is_", "order", "range"):
        getattr(q, m).return_value = q
    q.not_ = negated
    negated.is_.return_value = q
    q.execute.return_value = MagicMock(data=[{"url": "https://x/1"}, {"url": None}])
    with patch.object(hb_db, "get_supabase", return_value=q):
        out = real_requeued_urls("u1")
    assert out == ["https://x/1"]
    q.table.assert_called_with("handbacks")
    q.eq.assert_any_call("user_id", "u1")
    q.is_.assert_any_call("resolved_at", "null")
    # requeued rows only: requeued_at=not.is.null, never the plain is.null
    negated.is_.assert_called_with("requeued_at", "null")
    assert ("requeued_at", "null") not in [c.args for c in q.is_.call_args_list]
