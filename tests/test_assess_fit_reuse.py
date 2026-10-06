"""/tools/assess-fit reuses the queue's verdict — no second judge on the ATS walk.

Why these tests exist: the server judges Greenhouse/Lever/Ashby rows before the run and the
queue serves only those that cleared the user's bar (modules/fit_queue.py). The ATS walk then
called the live judge again on the page text, with no job id, so the server could not find
the verdict it already had. 10-02 the queue held Wikimedia 38 / Grafana 42 / Hightouch 38 on a
broad bar of 35; the live judge said 22-30, and three of five opened postings were skipped —
a second authority over one decision (#184's class). What must hold:

  * a row with a CURRENT stored verdict is decided by it — no model call — with the same
    clears_bar() the queue filtered with;
  * stale (fit_version != verdict_version: resume, mode, keywords or prompt changed), no
    score, or not this user's row -> the live judge, exactly as before;
  * no job_id (Indeed / ZipRecruiter walks) -> unchanged;
  * the company cap still runs first;
  * the row read is filtered by the caller's user_id (service_role bypasses RLS).
"""

from unittest.mock import patch

from modules.ai_fit_judge import clears_bar, verdict_version
from modules.fit_queue import build_queue

PROFILE = {"apply_mode": "broad", "keywords": ["marketing manager"]}
RESUME = "Marketing lead, 8 years."
CURRENT = verdict_version(PROFILE, RESUME)
LIVE = {"fit_score": 28, "decision": "skip", "reason": "live says no", "judged": True}


class _User:
    id = "u1"


def _row(score=38, version=CURRENT, **extra):
    return {
        "id": "job-1",
        "user_id": "u1",
        "title": "Head of Marketing",
        "company": "wikimedia",
        "fit_score": score,
        "fit_reason": "Strong marketing leadership match.",
        "fit_model": "claude-haiku",
        "fit_version": version,
        "fit_judged_at": "2026-10-01T06:15:00+00:00",
        **extra,
    }


def _assess(row, job_id="job-1", profile=PROFILE, history=()):
    from app.routers import tools
    from app.schemas import AssessFitRequest

    req = AssessFitRequest(
        job_title="Head of Marketing", company="Wikimedia", description="page text", job_id=job_id
    )
    with (
        patch.object(tools, "_assess_fit_gate", return_value=None),
        patch.object(tools.apps_db, "count_today", return_value=0),  # broad daily cap
        patch.object(tools, "user_day_start", return_value="2026-10-06T00:00:00+00:00"),
        patch.object(tools, "get_profile", return_value=dict(profile)),
        patch.object(tools, "resume_text_for", return_value=RESUME),
        patch("app.db.applications.companies_applied_since", return_value=list(history)),
        patch("app.db.handbacks.companies_handed_back_since", return_value=[]),
        patch("app.db.handbacks.requeued_companies", return_value=[]),
        patch.object(tools.jobs_db, "get_job_by_id", return_value=row) as read,
        patch.object(tools, "assess_fit", return_value=dict(LIVE)) as judge,
    ):
        return tools.assess_fit_endpoint(req, user=_User()), judge, read


def test_a_current_queue_verdict_decides_without_a_second_judge():
    out, judge, read = _assess(_row(38))
    judge.assert_not_called()
    read.assert_called_once_with("u1", "job-1")  # the caller's own pool only
    assert out["decision"] == "apply"
    assert out["fit_score"] == 38 and out["threshold"] == 35
    assert out["verdict_source"] == "queue"
    assert out["reason"] == "Strong marketing leadership match."
    assert out["judged"] is True


def test_a_current_verdict_below_the_bar_still_skips():
    out, judge, _ = _assess(_row(20))
    judge.assert_not_called()
    assert out["decision"] == "skip" and out["verdict_source"] == "queue"


def test_a_stale_verdict_goes_to_the_live_judge():
    # The resume (or mode, keywords, prompt) changed after scoring: the fingerprint differs.
    out, judge, _ = _assess(_row(38, version="judged-against-an-old-resume"))
    judge.assert_called_once()
    # The resume read for the staleness check is handed to the judge, not downloaded twice.
    assert judge.call_args.kwargs["resume_text"] == RESUME
    assert out["decision"] == "skip" and out["verdict_source"] == "live"
    assert out["fresh_because"] == "profile or resume changed since it was scored"


def test_a_mode_switch_makes_the_stored_verdict_stale():
    out, judge, _ = _assess(_row(38), profile={**PROFILE, "apply_mode": "standard"})
    judge.assert_called_once()
    assert out["verdict_source"] == "live"


def test_no_stored_score_goes_to_the_live_judge():
    out, judge, _ = _assess(_row(None, version=None))
    judge.assert_called_once()
    assert out["fresh_because"] == "no stored score"


def test_a_row_outside_the_callers_pool_goes_to_the_live_judge():
    # get_job_by_id filters by user_id: another user's id reads as None.
    out, judge, _ = _assess(None, job_id="someone-elses-row")
    judge.assert_called_once()
    assert out["verdict_source"] == "live" and out["fresh_because"] == "not in your list"
    assert "fit_reason" not in out


def test_the_native_walks_without_a_job_id_are_unchanged():
    out, judge, read = _assess(_row(38), job_id=None)
    read.assert_not_called()
    judge.assert_called_once()
    assert judge.call_args.kwargs["resume_text"] is None  # assess_fit reads it, as before
    assert out["verdict_source"] == "live" and "fresh_because" not in out


def test_an_unreadable_row_costs_a_judge_call_not_the_posting():
    from app.routers import tools
    from app.schemas import AssessFitRequest

    req = AssessFitRequest(job_title="Head of Marketing", company="Wikimedia", job_id="job-1")
    with (
        patch.object(tools, "_assess_fit_gate", return_value=None),
        patch.object(tools.apps_db, "count_today", return_value=0),  # broad daily cap
        patch.object(tools, "user_day_start", return_value="2026-10-06T00:00:00+00:00"),
        patch.object(tools, "get_profile", return_value=dict(PROFILE)),
        patch.object(tools, "_company_capped", return_value=False),
        patch.object(tools.jobs_db, "get_job_by_id", side_effect=RuntimeError("db down")),
        patch.object(tools, "assess_fit", return_value=dict(LIVE)) as judge,
    ):
        out = tools.assess_fit_endpoint(req, user=_User())
    judge.assert_called_once()
    assert out["verdict_source"] == "live"


def test_the_company_cap_still_runs_before_the_stored_verdict():
    out, judge, read = _assess(_row(90), history=["Wikimedia"])
    assert out["decision"] == "skip" and out.get("company_capped") is True
    judge.assert_not_called()
    read.assert_not_called()


def test_queue_and_reuse_share_one_bar():
    # A score exactly on the bar: the queue keeps it, so the walk must apply it.
    assert clears_bar(35, 35) and not clears_bar(34, 35) and not clears_bar(None, 0)
    q = build_queue([_row(35)], CURRENT, 35, [], 10)
    assert [r["id"] for r in q["jobs"]] == ["job-1"]
    out, judge, _ = _assess(_row(35))
    judge.assert_not_called()
    assert out["decision"] == "apply"
