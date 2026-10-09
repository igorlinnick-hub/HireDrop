"""/tools/assess-fit-batch — a search-results page judged before any posting is opened.

Why these tests exist: the Indeed walk opened every card and judged it live, ~18 s per
rejection, one at a time, and stored no verdict — 10-07 a run spent its whole Indeed share on
18 rejections in a row, and a third of all judge calls since 09-22 re-judged a posting already
rejected. The extension now sends the page's cards with their full text (Indeed's own
/rpc/jobdescs) and this endpoint decides them all at once. What must hold:

  * a CURRENT stored verdict decides with no model call (same fit_version rule as the queue);
  * the rest are judged and every verdict is STORED on the pool row;
  * company cap / already applied / Broad cap skip before any AI call;
  * anything that fails degrades to "unjudged" (the job page judges live), never to a skip;
  * cards the pool has not seen yet are inserted with their full text;
  * the posting text is stored on the row (text only), tags stripped.
"""

from unittest.mock import patch

from modules.ai_fit_judge import verdict_version

PROFILE = {"apply_mode": "broad", "keywords": ["marketing manager"]}
RESUME = "Marketing lead, 8 years."
CURRENT = verdict_version(PROFILE, RESUME)
LONG = "Lead our marketing team. " * 20


class _User:
    id = "u1"
    email = "someone@example.com"


def _card(n, company=None, text=LONG):
    return {
        "title": f"Marketing Manager {n}",
        "link": f"https://www.indeed.com/viewjob?jk=jk{n}",
        "company": company or f"Co{n}",
        "platform": "indeed",
        "description": text,
    }


def _row(n, **extra):
    return {
        "id": f"row-{n}",
        "link": f"https://www.indeed.com/viewjob?jk=jk{n}",
        "title": f"Marketing Manager {n}",
        "company": f"Co{n}",
        "status": "new",
        "description": "snippet",
        "date_found": "2026-10-07",
        "fit_score": None,
        "fit_version": None,
        **extra,
    }


def _run(
    cards,
    rows,
    *,
    scores=None,
    history=(),
    applied_today=0,
    profile=PROFILE,
    resume=RESUME,
    pool_error=False,
    budget_left=None,
    in_flight=(),
    keyword="",
):
    """Call the endpoint with the pool, the judge and the writes faked."""
    from app.routers import tools
    from app.schemas import AssessFitBatchRequest

    scores = scores or {}
    pool = {r["link"]: dict(r) for r in rows}
    inserted: list = []

    def rows_by_links(user_id, links):
        assert user_id == "u1"
        if pool_error:
            raise RuntimeError("PostgREST down")
        return {link: pool[link] for link in links if link in pool}

    def save_jobs_bulk(user_id, jobs, insert_only=False):
        assert insert_only, "the batch must never overwrite a row another request saved"
        for j in jobs:
            inserted.append(j)
            n = j["link"].rsplit("jk", 1)[1]
            pool[j["link"]] = _row(n, description=j["description"])
        return len(jobs)

    def judge(job=None, profile=None, resume_text=None, **_):
        n = job["title"].rsplit(" ", 1)[1]
        s = scores.get(n)
        if s == "raise":
            raise RuntimeError("model exploded")
        if s is None:
            return {"judged": False, "fail_closed": True, "decision": "skip"}
        return {"judged": True, "fit_score": s, "reason": f"score {s}", "judge_model": "haiku"}

    tools._assess_fit_counts.clear()
    if budget_left is not None:
        from datetime import date

        tools._assess_fit_counts["u1"] = {
            "day": date.today().isoformat(),
            "n": tools._ASSESS_FIT_DAILY_CAP - budget_left,
        }
    req = AssessFitBatchRequest(jobs=cards, keyword=keyword)
    from modules import fit_queue

    fit_queue._IN_FLIGHT.clear()
    fit_queue._IN_FLIGHT.update(in_flight)
    with (
        patch.object(tools, "is_admin", return_value=False),
        patch.object(tools, "get_profile", return_value=dict(profile)),
        patch("app.routers.jobs._deck_resume_text", return_value=resume),
        patch.object(tools.apps_db, "count_today", return_value=applied_today),
        patch.object(tools, "user_day_start", return_value="2026-10-07T00:00:00+00:00"),
        patch("app.db.applications.companies_applied_since", return_value=list(history)),
        patch("app.db.handbacks.companies_handed_back_since", return_value=[]),
        patch("app.db.handbacks.requeued_companies", return_value=[]),
        patch.object(tools.jobs_db, "rows_by_links", side_effect=rows_by_links),
        patch.object(tools.jobs_db, "save_jobs_bulk", side_effect=save_jobs_bulk),
        patch.object(tools.jobs_db, "update_job_description") as store_text,
        patch("app.db.jobs.save_fit_verdict", return_value=True) as store_verdict,
        patch("modules.ai_fit_judge.assess_fit", side_effect=judge) as model,
    ):
        out = tools.assess_fit_batch_endpoint(req, user=_User())
    fit_queue._IN_FLIGHT.clear()
    out["_charged"] = tools._assess_fit_counts.get("u1", {}).get("n", 0)
    return out, model, store_verdict, store_text, inserted


def _by_link(out):
    return {r["link"].rsplit("jk", 1)[1]: r for r in out["results"]}


def test_page_is_judged_in_one_call_and_every_verdict_is_stored():
    out, model, store_verdict, _, _ = _run(
        [_card(1), _card(2), _card(3)],
        [_row(1), _row(2), _row(3)],
        scores={"1": 60, "2": 12, "3": 35},
    )
    r = _by_link(out)
    assert r["1"]["decision"] == "apply" and r["1"]["fit_score"] == 60
    assert r["2"]["decision"] == "skip" and r["2"]["reason"] == "score 12"
    assert r["3"]["decision"] == "apply"  # 35 is the broad bar itself
    assert r["1"]["job_id"] == "row-1" and r["1"]["source"] == "judged"
    assert model.call_count == 3
    stored = {c.args[0]: (c.args[1], c.args[2], c.args[5]) for c in store_verdict.call_args_list}
    assert stored == {
        "row-1": ("u1", 60, CURRENT),
        "row-2": ("u1", 12, CURRENT),
        "row-3": ("u1", 35, CURRENT),
    }
    assert out["judged"] == 3 and out["reused"] == 0 and out["unjudged"] == 0


def test_a_current_stored_verdict_is_reused_without_a_model_call():
    rows = [
        _row(1, fit_score=18, fit_version=CURRENT, fit_reason="construction PM"),
        _row(2, fit_score=50, fit_version="stale-version"),
    ]
    out, model, _, _, _ = _run([_card(1), _card(2)], rows, scores={"2": 41})
    r = _by_link(out)
    assert r["1"]["decision"] == "skip" and r["1"]["source"] == "stored"
    assert r["1"]["reason"] == "construction PM"
    # A verdict reached under another resume/profile/prompt is judged again.
    assert r["2"]["decision"] == "apply" and r["2"]["source"] == "judged"
    assert model.call_count == 1
    assert out["reused"] == 1


def test_company_cap_and_applied_skip_before_any_model_call():
    rows = [_row(1), _row(2, status="applied"), _row(3)]
    out, model, _, _, _ = _run(
        [_card(1, company="DoorDash, Inc."), _card(2), _card(3)],
        rows,
        scores={"3": 70},
        history=["doordashusa"],
    )
    r = _by_link(out)
    assert r["1"]["decision"] == "skip" and r["1"]["source"] == "company_cap"
    assert r["1"]["reason"].startswith("Company cap")
    assert r["2"]["decision"] == "skip" and r["2"]["source"] == "applied"
    assert r["3"]["decision"] == "apply"
    assert model.call_count == 1


def test_broad_daily_cap_skips_the_whole_page_without_reading_the_pool():
    out, model, _, _, _ = _run([_card(1), _card(2)], [], applied_today=40, pool_error=True)
    assert {r["decision"] for r in out["results"]} == {"skip"}
    assert {r["source"] for r in out["results"]} == {"broad_cap"}
    model.assert_not_called()


def test_a_judge_failure_is_unjudged_never_a_skip():
    out, _, store_verdict, _, _ = _run([_card(1), _card(2)], [_row(1), _row(2)], scores={"1": 50})
    r = _by_link(out)
    assert r["1"]["decision"] == "apply"
    assert r["2"]["decision"] == "unjudged"  # the job page judges it live
    assert [c.args[0] for c in store_verdict.call_args_list] == ["row-1"]


def test_an_unreadable_pool_sends_every_card_back_unjudged():
    out, model, _, _, _ = _run([_card(1)], [], pool_error=True)
    assert out["results"][0]["decision"] == "unjudged"
    model.assert_not_called()


def test_no_resume_means_no_verdicts_not_confident_rejections():
    out, model, store_verdict, _, _ = _run([_card(1)], [_row(1)], scores={"1": 5}, resume="")
    assert out["results"][0]["decision"] == "unjudged"
    model.assert_not_called()
    store_verdict.assert_not_called()


def test_spent_daily_budget_leaves_the_rest_unjudged():
    out, model, _, _, _ = _run(
        [_card(1), _card(2), _card(3)],
        [_row(1), _row(2), _row(3)],
        scores={"1": 50, "2": 50, "3": 50},
        budget_left=1,
    )
    assert model.call_count == 1
    assert sorted(r["decision"] for r in out["results"]) == ["apply", "unjudged", "unjudged"]


def test_cards_the_pool_has_not_seen_are_inserted_with_their_full_text():
    out, _, _, _, inserted = _run([_card(7)], [], scores={"7": 44})
    assert [j["link"] for j in inserted] == ["https://www.indeed.com/viewjob?jk=jk7"]
    assert inserted[0]["description"].startswith("Lead our marketing team.")
    assert inserted[0]["status"] == "new"
    assert out["results"][0]["decision"] == "apply"
    assert out["results"][0]["job_id"] == "row-7"


def test_the_posting_text_is_stored_without_tags_and_only_when_longer():
    html_text = "<p><b>Own the brand.</b></p><ul><li>Lead campaigns &amp; launches</li></ul>" * 10
    rows = [_row(1), _row(2, description="x" * 4000)]
    _, _, _, store_text, _ = _run(
        [_card(1, text=html_text), _card(2, text="short")],
        rows,
        scores={"1": 50, "2": 50},
    )
    calls = {c.args[0]: c.args[2] for c in store_text.call_args_list}
    assert set(calls) == {"row-1"}  # row 2 already holds the longer text
    assert "<" not in calls["row-1"] and "Lead campaigns & launches" in calls["row-1"]
    assert all(c.args[1] == "u1" for c in store_text.call_args_list)


def test_only_harvest_platforms_and_one_result_per_link():
    cards = [_card(1), _card(1), {**_card(2), "platform": "greenhouse"}]
    out, _, _, _, _ = _run(cards, [_row(1)], scores={"1": 50})
    assert [r["link"] for r in out["results"]] == ["https://www.indeed.com/viewjob?jk=jk1"]


def test_rows_the_person_closed_or_picked_are_never_judged():
    rows = [_row(1, status="skipped"), _row(2, status="rejected"), _row(3, status="approved")]
    out, model, _, _, _ = _run(
        [_card(1), _card(2), _card(3)], rows, scores={"1": 90, "2": 90, "3": 90}
    )
    r = _by_link(out)
    assert r["1"]["decision"] == "skip" and r["1"]["source"] == "dismissed"
    assert r["2"]["decision"] == "skip" and r["2"]["source"] == "dismissed"
    # A Tap "yes" is opened and decided on its page, as before — never vetoed here.
    assert r["3"]["decision"] == "unjudged" and r["3"]["source"] == "picked"
    assert r["3"]["job_id"] == "row-3"
    model.assert_not_called()


def test_a_snippet_is_never_judged_or_stored():
    out, model, store_verdict, _, _ = _run(
        [_card(1, text="short card")], [_row(1)], scores={"1": 80}
    )
    assert out["results"][0]["decision"] == "unjudged"
    assert out["results"][0]["source"] == "thin_text"
    model.assert_not_called()
    store_verdict.assert_not_called()


def test_budget_is_charged_only_for_calls_that_started():
    # No resume: judge_pending makes no call -> nothing charged.
    out, _, _, _, _ = _run([_card(1), _card(2)], [_row(1), _row(2)], scores={"1": 50}, resume="")
    assert out["_charged"] == 0
    # A row another request is judging right now: no call here, no charge for it.
    out, model, _, _, _ = _run(
        [_card(1), _card(2)], [_row(1), _row(2)], scores={"1": 50, "2": 50}, in_flight={"row-2"}
    )
    assert model.call_count == 1 and out["_charged"] == 1
    assert _by_link(out)["2"]["decision"] == "unjudged"


def test_a_call_that_stored_no_verdict_is_refunded():
    # Card 2's judge call fails closed: nothing stored, the card goes back "unjudged",
    # and the job page will judge it live — a second budget claim for the same posting.
    # The first claim is handed back, so one verdict never costs two units (#387 tail).
    out, model, store_verdict, _, _ = _run(
        [_card(1), _card(2)], [_row(1), _row(2)], scores={"1": 50}
    )
    assert model.call_count == 2  # both calls really ran
    assert [c.args[0] for c in store_verdict.call_args_list] == ["row-1"]
    assert out["_charged"] == 1  # …but only the one that produced a verdict is paid for


def test_a_call_that_raised_is_refunded_too():
    # The other no-verdict shape: the judge call raised (network, model error) instead
    # of failing closed. Same economics — nothing stored, the live judge pays again.
    out, model, store_verdict, _, _ = _run(
        [_card(1), _card(2)], [_row(1), _row(2)], scores={"1": 50, "2": "raise"}
    )
    assert model.call_count == 2
    assert [c.args[0] for c in store_verdict.call_args_list] == ["row-1"]
    assert out["_charged"] == 1
    assert _by_link(out)["2"]["decision"] == "unjudged"


def test_a_row_this_request_inserted_gets_its_text_written_again_after_the_judge():
    # The harvest's snippet insert can land between our insert and our judge; the full
    # text is re-asserted after the judge for rows this request created.
    _, _, _, store_text, inserted = _run([_card(9)], [], scores={"9": 50})
    assert len(inserted) == 1
    assert [c.args[0] for c in store_text.call_args_list] == ["row-9"]


def test_a_full_house_sends_the_page_back_unjudged():
    from app.routers import tools

    taken = 0
    while tools._BATCH_SLOTS.acquire(blocking=False):
        taken += 1
    try:
        out, model, _, _, _ = _run([_card(1)], [_row(1)], scores={"1": 50})
    finally:
        for _ in range(taken):
            tools._BATCH_SLOTS.release()
    assert out["results"][0]["decision"] == "unjudged" and out["results"][0]["source"] == "busy"
    model.assert_not_called()


def test_a_stored_verdict_on_the_job_page_costs_no_budget():
    from app.routers import tools
    from app.schemas import AssessFitRequest

    row = _row(1, fit_score=50, fit_version=CURRENT, fit_reason="fits", user_id="u1")
    tools._assess_fit_counts.clear()
    req = AssessFitRequest(job_title="Marketing Manager 1", company="Co1", job_id="row-1")
    with (
        patch.object(tools, "get_profile", return_value=dict(PROFILE)),
        patch.object(tools, "resume_text_for", return_value=RESUME),
        patch.object(tools.apps_db, "count_today", return_value=0),
        patch.object(tools, "user_day_start", return_value="2026-10-07T00:00:00+00:00"),
        patch("app.db.applications.companies_applied_since", return_value=[]),
        patch("app.db.handbacks.companies_handed_back_since", return_value=[]),
        patch.object(tools.jobs_db, "get_job_by_id", return_value=row),
        patch.object(tools, "assess_fit") as model,
        patch.object(tools, "is_admin", return_value=False),
    ):
        out = tools.assess_fit_endpoint(req, user=_User())
    assert out["decision"] == "apply" and out["verdict_source"] == "queue"
    model.assert_not_called()
    assert tools._assess_fit_counts.get("u1", {}).get("n", 0) == 0
