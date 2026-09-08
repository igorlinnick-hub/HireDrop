"""One posting = one identity, however the URL was written.

The pool keys on the exact link string, so the same job arriving under a second spelling
becomes a second row — and the first one, the one the user actually swiped, stays
`approved` forever. Live pair from Igor's pool 2026-09-08 pinned below.
"""

from unittest.mock import MagicMock, patch

from modules.job_identity import job_identity, same_posting


def test_the_live_greenhouse_pair_is_one_posting():
    # The exact two rows that split: different host, one carries ?gh_jid.
    a = "https://boards.greenhouse.io/braze/jobs/8095311?gh_jid=8095311"
    b = "https://job-boards.greenhouse.io/braze/jobs/8095311"
    assert same_posting(a, b) is True
    assert job_identity(a) == "greenhouse:8095311"


def test_neighbouring_postings_stay_distinct():
    # Braze had four near-identical rows; ids one apart must never collapse.
    a = "https://boards.greenhouse.io/braze/jobs/8095311?gh_jid=8095311"
    b = "https://boards.greenhouse.io/braze/jobs/8095314?gh_jid=8095314"
    assert same_posting(a, b) is False


def test_apply_page_is_not_another_job():
    base = "https://jobs.ashbyhq.com/ramp/2e07fb9f-fefd-44a4-a6ef-235b4e567432"
    assert same_posting(base, base + "/application") is True
    assert same_posting(base, base + "/") is True
    lever = "https://jobs.lever.co/acme/9f8e7d6c-1234-4321-abcd-0123456789ab"
    assert same_posting(lever, lever + "/apply") is True


def test_indeed_identity_is_the_query_not_the_path():
    # Every Indeed job shares /viewjob — path-based identity would merge them all.
    a = "https://www.indeed.com/viewjob?jk=890abcdef0123456&from=serp"
    b = "https://smartapply.indeed.com/beta/indeedapply/form?jk=890abcdef0123456"
    assert same_posting(a, b) is True
    assert same_posting(a, "https://www.indeed.com/viewjob?jk=111122223333aaaa") is False


def test_unknown_identity_never_counts_as_a_match():
    # No id = no dedup. Guessing here would mark unrelated rows applied.
    assert job_identity("https://example.com/careers") is None
    assert job_identity("") is None
    assert same_posting("https://example.com/jobs/x", "https://example.com/jobs/y") is False


# --- healing the twins at apply time -------------------------------------------------


def _fake_supabase(rows):
    fake = MagicMock()
    fake.table.return_value.select.return_value.eq.return_value.ilike.return_value.execute.return_value.data = rows
    return fake


def test_apply_closes_out_the_twin_row_the_user_swiped():
    from app.db import jobs as jobs_db

    rows = [
        {
            "id": "swiped",
            "link": "https://boards.greenhouse.io/braze/jobs/8095311?gh_jid=8095311",
            "status": "approved",
        },
        {
            "id": "other",
            "link": "https://boards.greenhouse.io/braze/jobs/8095314?gh_jid=8095314",
            "status": "new",
        },
        {
            "id": "done",
            "link": "https://job-boards.greenhouse.io/braze/jobs/8095311",
            "status": "applied",
        },
    ]
    fake = _fake_supabase(rows)
    with patch("app.db.jobs.get_supabase", return_value=fake):
        healed = jobs_db.mark_applied_by_link(
            "u1", "https://job-boards.greenhouse.io/braze/jobs/8095311", "applied"
        )

    assert healed == 1  # only the twin; the neighbouring id and the finished row untouched
    updated_ids = fake.table.return_value.update.return_value.in_.call_args.args[1]
    assert updated_ids == ["swiped"]


def test_healing_is_a_no_op_when_the_url_has_no_identity():
    from app.db import jobs as jobs_db

    fake = MagicMock()
    with patch("app.db.jobs.get_supabase", return_value=fake):
        assert jobs_db.mark_applied_by_link("u1", "https://example.com/careers") == 0
    fake.table.assert_not_called()
