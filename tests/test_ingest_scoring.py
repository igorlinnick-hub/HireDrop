"""Harvested cards get scored when — and only when — there is something to score.

Indeed is a third of the pool (425 rows) and a VERIFIED platform, and every row sat at
`score = null` forever. The old docstring promised "a description-bearing pass rescores
them" later; no such pass exists (`_backfill_thin_ats_scores` covers greenhouse and lever
only), so the rows sorted last permanently.

The fix is not to score harder — it is to harvest the card's own snippet, which is already
in the search DOM and costs no extra page load. Ranking has to happen BEFORE the user
swipes: enriching at apply time would produce a score for a job we already applied to.

Title-only cards stay unscored on purpose. #192 measured what happens otherwise: with no
description the model scores the title match, and scores it HIGH.
"""

from unittest.mock import MagicMock, patch

from app.routers.jobs import MIN_SCORABLE_DESC

SNIPPET = (
    "Own delivery for a distributed platform team, run quarterly planning, and partner "
    "with vendors on integration timelines. 5+ years of project management required."
)


def _ingest(client, jobs):
    return client.post("/api/v1/jobs/ingest", json={"jobs": jobs})


def _card(**over):
    card = {
        "title": "Project Manager",
        "company": "Corvant",
        "link": "https://www.indeed.com/viewjob?jk=abc123",
        "platform": "indeed",
    }
    card.update(over)
    return card


def test_card_with_a_snippet_is_scored(auth_client):
    def fake_batch(rows, _profile, _resume):
        for r in rows:
            r["score"] = 6
        return rows

    save = MagicMock(return_value=1)
    with (
        patch("app.routers.jobs.jobs_db.existing_links", return_value=set()),
        patch("app.routers.jobs.jobs_db.save_jobs_bulk", save),
        patch("app.db.profile.get_profile", return_value={"resume_url": None}),
        patch("modules.ai_cover_letter.load_resume_text", return_value="resume"),
        patch("modules.ai_job_scorer.score_jobs_batch", side_effect=fake_batch),
    ):
        res = _ingest(auth_client, [_card(description=SNIPPET)])

    assert res.json()["scored"] == 1
    assert res.json()["unscored_title_only"] == 0
    # The score has to reach the row that gets written, not just the response.
    assert save.call_args.args[1][0]["score"] == 6


def test_title_only_card_is_saved_but_not_scored(auth_client):
    batch = MagicMock()
    save = MagicMock(return_value=1)
    with (
        patch("app.routers.jobs.jobs_db.existing_links", return_value=set()),
        patch("app.routers.jobs.jobs_db.save_jobs_bulk", save),
        patch("modules.ai_job_scorer.score_jobs_batch", batch),
    ):
        res = _ingest(auth_client, [_card()])

    assert res.json() == {
        "saved": 1,
        "skipped_existing": 0,
        "scored": 0,
        "unscored_title_only": 1,
    }
    batch.assert_not_called(), "scoring a bare title is the trap, not the feature"


def test_a_snippet_too_short_to_judge_is_not_scored(auth_client):
    """Below the floor we are scoring a title with decoration again."""
    batch = MagicMock()
    with (
        patch("app.routers.jobs.jobs_db.existing_links", return_value=set()),
        patch("app.routers.jobs.jobs_db.save_jobs_bulk", return_value=1),
        patch("modules.ai_job_scorer.score_jobs_batch", batch),
    ):
        res = _ingest(auth_client, [_card(description="x" * (MIN_SCORABLE_DESC - 1))])

    assert res.json()["scored"] == 0
    assert res.json()["unscored_title_only"] == 1
    batch.assert_not_called()


def test_scoring_failure_never_costs_the_harvest(auth_client):
    """The rows are the point; the score is an improvement on top of them."""
    save = MagicMock(return_value=1)
    with (
        patch("app.routers.jobs.jobs_db.existing_links", return_value=set()),
        patch("app.routers.jobs.jobs_db.save_jobs_bulk", save),
        patch("app.db.profile.get_profile", return_value={"resume_url": None}),
        patch("modules.ai_cover_letter.load_resume_text", return_value="resume"),
        patch("modules.ai_job_scorer.score_jobs_batch", side_effect=RuntimeError("anthropic down")),
    ):
        res = _ingest(auth_client, [_card(description=SNIPPET)])

    assert res.status_code == 200
    assert res.json()["saved"] == 1
    assert res.json()["scored"] == 0
    save.assert_called_once()


def test_counters_distinguish_a_regression_from_a_quiet_day(auth_client):
    """'saved 2, scored 0' must be readable as "the extension stopped sending snippets"."""

    def fake_batch(rows, _profile, _resume):
        for r in rows:
            r["score"] = 4
        return rows

    with (
        patch("app.routers.jobs.jobs_db.existing_links", return_value=set()),
        patch("app.routers.jobs.jobs_db.save_jobs_bulk", return_value=2),
        patch("app.db.profile.get_profile", return_value={"resume_url": None}),
        patch("modules.ai_cover_letter.load_resume_text", return_value="resume"),
        patch("modules.ai_job_scorer.score_jobs_batch", side_effect=fake_batch),
    ):
        res = _ingest(
            auth_client,
            [
                _card(description=SNIPPET),
                _card(link="https://www.indeed.com/viewjob?jk=def456"),
            ],
        )

    body = res.json()
    assert body["scored"] == 1
    assert body["unscored_title_only"] == 1


def test_existing_links_are_still_skipped_entirely(auth_client):
    """INSERT-only is the dedup lane: a re-harvest must not rescore or resurrect."""
    batch = MagicMock()
    save = MagicMock(return_value=0)
    link = "https://www.indeed.com/viewjob?jk=abc123"
    with (
        patch("app.routers.jobs.jobs_db.existing_links", return_value={link}),
        patch("app.routers.jobs.jobs_db.save_jobs_bulk", save),
        patch("modules.ai_job_scorer.score_jobs_batch", batch),
    ):
        res = _ingest(auth_client, [_card(link=link, description=SNIPPET)])

    assert res.json()["skipped_existing"] == 1
    assert res.json()["scored"] == 0
    batch.assert_not_called()
    assert save.call_args.args[1] == []
